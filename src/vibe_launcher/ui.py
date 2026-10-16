"""PySide6 desktop interface for the Vibe Launcher.

Layout: collapsible Sidebar + main workspace. All behaviour (store/service calls,
SSH/credential/CLI actions) lives on ``VibeLauncherWindow``, with the sessions page
(``ui_sessions``) and GitHub/Harness/quota (``ui_integrations``) mixed in; the visual components
come from ``kit`` / ``env_page`` and all styling from ``theme`` tokens.
"""

from __future__ import annotations

from .i18n import join_names, tr

from .i18n import tr

import json
import os
import logging
from logging.handlers import RotatingFileHandler
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QProcess, QProcessEnvironment, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QScrollArea,
    QStackedWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from . import env_state, icons, remote_browse, selfcmd, theme
from . import runner as runner_module
from . import terminal as terminal_module
from . import runtime as runtime_module
from .env_page import EnvironmentsPage
from .accounts_page import AccountsPage
from .launch_dialogs import AccountChoiceDialog, CreateFoldersDialog, PathAssist, RemoteFolderDialog, TagAssist
from .quota_panel import QuotaPanel
from .drafts import FormDraft
from .store import normalize_tags
from .tag_bar import TagFilterBar, collect_tags, matches_tags, project_tags
from .kit import (
    ActionMenu,
    apply_fonts,
    Command,
    CommandPalette,
    ConfirmDialog,
    EmptyState,
    FactList,
    FilterBar,
    FlowLayout,
    IconButton,
    MenuAction,
    PageHeader,
    BADGE_ROLE,
    ICON_ROLE,
    IconTile,
    SearchField,
    Sidebar,
    ToastManager,
    TwoLineDelegate,
    dispose_widget,
    label as text_label,
    later,
    make_button,
    retheme_widgets,
)
from .appearance import AppearanceDialog
from .update_dialog import UpdateDialog
from .theme import C, R, S, Size, T, TOOL_ICONS, section_hue

TOOL_NAMES = {"codex": "Codex", "claude": "Claude", "deepseek-harness": tr('ui.f241687ec5a7')}
from .work_drawer import WorkDrawer, WorkRow
from . import project_icons
from . import tray as tray_module
from .workspace_page import DEFAULT_ICON_KEY, WORKSPACE_GLYPH, WorkspacePage
from .ui_format import _display_time, _value
from .ui_integrations import IntegrationActions
from .ui_sessions import SessionActions
from .ui_quota import QuotaActions

# Projects page workspace filter values that are not desktop ids ("" is the default desktop; saved ids never contain "@").
ALL_WORKSPACES = "@all"
NO_WORKSPACE = "@none"


class RecordDialog(QDialog):
    """Small metadata-only editor. Secrets are deliberately never requested.

    ``validate(values)`` returns an error message ("" when fine); a refused save stays open with the message under
    the form, so nothing typed is lost.
    """

    def __init__(self, title: str, fields: list[tuple[str, str, str]], values: dict[str, Any] | None = None,
                 choices: dict[str, list[tuple[str, str]]] | None = None, parent: QWidget | None = None,
                 checkboxes: list[tuple[str, str]] | None = None, draft: bool = False,
                 validate: Any = None):
        super().__init__(parent)
        self._validate = validate
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self.fields: dict[str, QWidget] = {}
        values = values or {}
        choices = choices or {}
        root = QVBoxLayout(self)
        root.setContentsMargins(S.xl, S.xl, S.xl, S.lg)
        root.setSpacing(S.lg)
        heading = QLabel(title)
        heading.setProperty("role", "dialogTitle")
        root.addWidget(heading)
        self.form = form = QFormLayout()
        form.setSpacing(S.md)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for key, label, placeholder in fields:
            if key in choices:
                widget = QComboBox()
                for text, value in choices[key]:
                    widget.addItem(text, value)
                current = values.get(key)
                index = widget.findData(current)
                if index >= 0:
                    widget.setCurrentIndex(index)
            else:
                widget = QLineEdit(_value(values, key))
                widget.setPlaceholderText(placeholder)
            widget.setAccessibleName(label)
            self.fields[key] = widget
            form.addRow(label, widget)
        for key, label in checkboxes or []:
            widget = QCheckBox(label)
            widget.setChecked(bool(values.get(key, False)))
            self.fields[key] = widget
            form.addRow(widget)
        root.addLayout(form)
        self.error = QLabel("")
        self.error.setProperty("tone", "danger")
        self.error.setWordWrap(True)
        self.error.hide()
        root.addWidget(self.error)
        self.draft = None
        store = getattr(parent, "store", None)
        if draft and store is not None:
            # What was typed survives closing the dialog by accident or a save that is refused afterwards.
            self.draft = FormDraft(self, store.path.parent, f"record:{title}:{values.get('id') or 'new'}", self.fields)
            if self.draft.restored:
                note = QHBoxLayout()
                text = QLabel(tr('ui.27646dd50abf'))
                text.setProperty("role", "muted")
                note.addWidget(text, 1)
                note.addWidget(make_button(tr('ui.20df279f0144'), "ghost", small=True, callback=lambda: self._discard(values)))
                root.addLayout(note)
        buttons = QHBoxLayout()
        buttons.setSpacing(S.sm)
        buttons.addStretch(1)
        cancel = make_button(tr('ui.2cd0f3be8738'), "ghost")
        save = make_button(tr('ui.dba44ed3f54f'), "primary")
        save.setDefault(True)
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        root.addLayout(buttons)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        theme.apply_dark_titlebar(self)

    def accept(self) -> None:
        problem = self._validate(self.values()) if self._validate else ""
        if problem:
            self.error.setText(problem)
            self.error.show()
            return
        super().accept()

    def done(self, result: int) -> None:  # noqa: D401 - Qt override
        if self.draft is not None:
            self.draft.save()          # flush the pending write; the caller clears it once the record is saved
        super().done(result)

    def _discard(self, values: dict[str, Any]) -> None:
        for key, widget in self.fields.items():
            if isinstance(widget, QComboBox):
                widget.setCurrentIndex(max(0, widget.findData(values.get(key))))
            elif isinstance(widget, QLineEdit):
                widget.setText(_value(values, key))
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(values.get(key, False)))
        if self.draft is not None:
            self.draft.clear()

    def discard_draft(self) -> None:
        if self.draft is not None:
            self.draft.clear()

    def values(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, widget in self.fields.items():
            if isinstance(widget, QComboBox):
                result[key] = widget.currentData()
            elif isinstance(widget, QLineEdit):
                result[key] = widget.text().strip()
            elif isinstance(widget, QCheckBox):
                result[key] = widget.isChecked()
        return result


class AdaptiveSplit(QWidget):
    """Two panes side by side; stacked vertically when the window gets narrow."""

    def __init__(self, first: QWidget, second: QWidget, first_stretch: int = 3, second_stretch: int = 2,
                 breakpoint: int = 820, parent: QWidget | None = None):
        super().__init__(parent)
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(S.lg)
        self._layout.addWidget(first, first_stretch)
        self._layout.addWidget(second, second_stretch)
        self._breakpoint = breakpoint
        self._stretch = (first_stretch, second_stretch)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        wide = self.width() >= self._breakpoint
        direction = QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom
        if self._layout.direction() != direction:
            self._layout.setDirection(direction)
            # side by side: list gets more room; stacked: give both panes equal height
            for index, stretch in enumerate(self._stretch if wide else (1, 1)):
                self._layout.setStretch(index, stretch)


class ListPanel(QFrame):
    """Flat surface holding a list and an empty state (toggled by ``show_empty``)."""

    def __init__(self, listing: QListWidget, empty: EmptyState, parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("surface", "card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(S.sm, S.sm, S.sm, S.sm)
        self.stack = QStackedWidget()
        self.stack.addWidget(listing)
        self.stack.addWidget(empty)
        layout.addWidget(self.stack)
        self.listing = listing
        self.empty = empty
        listing.setFrameShape(QFrame.Shape.NoFrame)
        listing.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        listing.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        listing.setItemDelegate(TwoLineDelegate(listing))
        listing.setMouseTracking(True)
        listing.setSpacing(2)
        listing.setWordWrap(False)

    def show_empty(self, flag: bool) -> None:
        self.stack.setCurrentWidget(self.empty if flag else self.listing)


class ProjectIconClick(QObject):
    """Makes the icon drawn in a project row clickable (it opens the icon picker); the rest of the row behaves as before."""

    clicked = Signal(object)           # the QListWidgetItem whose icon was clicked
    HIT = (12, 30)                     # x offset and size of the tile TwoLineDelegate draws, plus a little slack

    def __init__(self, listing: QListWidget):
        super().__init__(listing)
        self.listing = listing
        listing.viewport().installEventFilter(self)

    def _item_at(self, pos: QPoint):
        item = self.listing.itemAt(pos)
        glyph = item.data(ICON_ROLE) if item else None
        if not glyph or glyph[0] != "@project":
            return None
        rect = self.listing.visualItemRect(item)
        x, size = self.HIT
        tile = rect.adjusted(2, 1, -2, -1)
        left, top = tile.left() + x - 3, tile.center().y() - size // 2 - 3
        return item if left <= pos.x() <= left + size + 6 and top <= pos.y() <= top + size + 6 else None

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        kind = event.type()
        if kind == QEvent.Type.MouseMove:
            over = self._item_at(event.position().toPoint()) is not None
            self.listing.viewport().setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.ArrowCursor)
            if over:
                QToolTip.showText(event.globalPosition().toPoint(), tr("Change icon…"), self.listing.viewport())
        elif kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            item = self._item_at(event.position().toPoint())
            if item is not None:
                self.clicked.emit(item)
                return True
        elif kind == QEvent.Type.MouseButtonDblClick and self._item_at(event.position().toPoint()) is not None:
            return True                # a double click on the icon must not also launch the project
        return False


class ProjectIconTile(IconTile):
    """The project's icon in its details header: click (or Enter / Space) to change it."""

    clicked = Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(tr("Change icon…"))
        self.setAccessibleName(tr("Change icon…"))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(event)


def _surface(layout: QVBoxLayout | QHBoxLayout, margins: int = S.xl) -> QFrame:
    frame = QFrame()
    frame.setProperty("surface", "card")
    frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    layout.setContentsMargins(margins, margins, margins, margins)
    frame.setLayout(layout)
    return frame


class VibeLauncherWindow(SessionActions, IntegrationActions, QuotaActions, QMainWindow):
    PAGES = ("projects", "workspace", "accounts", "environments", "sessions", "home")   # Ctrl+1..5 keep their pages
    EMBEDDED_ACTIONS = {"launch", "attach", "login", "quota"}
    UPDATE_INTERVAL_MS = 6 * 3600 * 1000
    UPDATE_AUTOCHECK = True
    _quota_done = Signal(str, str, str)       # account id, environment id, error text ("" on success)
    _update_done = Signal(dict)
    update_report_changed = Signal()

    def __init__(self, store: Any, service: Any, terminal_factory: Any = None):
        super().__init__()
        self.store = store
        self.service = service
        self._terminal_factory = terminal_factory
        self.prefs = _load_prefs(store)
        self._tray = None                       # created the first time the window is closed to the tray
        self._quitting = False                  # set while quitting for real (tray Quit, installer update)
        theme.apply_theme(self.prefs.theme, self.prefs.accent)
        theme.set_fonts(self.prefs.ui_font, self.prefs.terminal_font)
        apply_fonts((theme.families(), theme.families(mono=True)))      # the application default for widgets created next
        terminal_module.configure(font_size=self.prefs.terminal_font_size, cursor_blink=self.prefs.cursor_blink)
        project_icons.configure(store.path.parent, self.prefs.project_icons)
        from . import updates
        self.setWindowTitle(f"Vibe Launcher {updates.current_version()}")
        self.resize(1180, 760)
        self.setMinimumSize(640, 520)
        self._records: dict[str, list[dict[str, Any]]] = {}
        self._selected_project: str | None = None
        self._project_env_filter: str | None = None
        self._pages: dict[str, QWidget] = {}
        self._pending: dict[str, dict[str, dict[str, Any]]] = {}
        self._quota_busy: dict[tuple[str, str], dict[str, Any]] = {}
        self._quota_notes: dict[tuple[str, str], str] = {}
        self._operations: dict[str, dict] = {}
        self._last_poll_error = 0.0
        self._report_signature = None
        self._update_busy = False
        self._update_report: dict[str, Any] | None = None
        from .harness import TunnelManager
        self.tunnels = TunnelManager(store.path.parent)
        self._build()
        self.reload()
        self._restore_last_desktop()
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._poll)
        self.timer.start()

    # ------------------------------------------------------------------ build
    def _prime_gpu_composition(self) -> None:
        """Start the window in GPU composition, the way it will be once a built-in terminal is open.

        Qt switches a shown window to GPU composition when the first web view joins it, and does that by destroying
        and re-creating the native window: the whole launcher vanishes for a moment as if it restarted (typically when
        re-attaching to work). A 1x1 Quick widget, created before the window is shown, makes the switch up front.
        """
        if self._terminal_factory is not None or not terminal_module.available():
            return
        try:
            from PySide6.QtQuickWidgets import QQuickWidget
        except ImportError:
            return
        primer = QQuickWidget(self.centralWidget())
        primer.setObjectName("gpuPrimer")
        primer.setClearColor(QColor(C.bg))
        primer.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        primer.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        primer.setGeometry(0, 0, 1, 1)
        primer.lower()                 # under the sidebar: never seen
        self._gpu_primer = primer

    def _apply_window_icon(self) -> None:
        """Window, dialog and taskbar icon; the robots' eyes take the theme accent."""
        try:
            icon = icons.logo_icon(C.accent)
        except OSError:
            return          # logo asset missing: keep the platform default rather than fail to start
        app = QApplication.instance()
        if app is not None:
            app.setWindowIcon(icon)
        self.setWindowIcon(icon)

    def _build(self) -> None:
        self.setStyleSheet(theme.build_stylesheet())
        self._apply_window_icon()
        self.setFont(theme.font())
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.sidebar = Sidebar([("projects", tr('ui.e564b916b12e'), "folder"), ("workspace", tr('ui.1c5ab76e581d'), "square-terminal"),
                                ("accounts", tr('ui.969396f78f3b'), "user"), ("environments", tr('ui.fba18681418f'), "layers"),
                                ("sessions", tr('ui.eda135194fbc'), "activity")])
        self.sidebar.set_collapsed(self.prefs.sidebar_collapsed, animate=False, emit=False)
        self.sidebar.navigate.connect(self.show_page)
        self.sidebar.import_requested.connect(self.import_config)
        self.sidebar.export_requested.connect(self.export_config)
        self.sidebar.appearance_requested.connect(self.open_settings)
        self.sidebar.home_requested.connect(lambda: self.show_page("home"))
        self.sidebar.collapse_toggled.connect(self._remember_sidebar)
        self.nav_buttons = self.sidebar.buttons

        self.stack = QStackedWidget()
        self._pages["projects"] = self._build_projects()
        self._pages["workspace"] = self._build_workspace()
        self._pages["accounts"] = self._build_accounts()
        self._pages["environments"] = self._build_environments()
        self._pages["sessions"] = self._build_sessions()
        self._pages["home"] = self._build_home()
        for key in self.PAGES:
            self.stack.addWidget(self._pages[key])
        outer.addWidget(self.sidebar)
        outer.addWidget(self.stack, 1)

        anchor = getattr(self.prefs, "toast_anchor", None)
        self.toasts = ToastManager(central, tuple(anchor) if anchor else None)
        self.toasts.moved.connect(self._remember_toast_anchor)
        self.palette = CommandPalette(self)
        self._install_shortcuts()
        self.show_page("home")
        self._apply_mode()
        self._prime_gpu_composition()
        later(self, 700, self._startup_housekeeping)
        self._update_done.connect(self._on_update_checked)
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(self.UPDATE_INTERVAL_MS)
        self._update_timer.timeout.connect(lambda: self.check_updates(force=False))
        self._update_timer.start()
        later(self, 5000, lambda: self.check_updates(force=False))

    FINISHED_KEPT = 20

    def _startup_housekeeping(self) -> None:
        """Tidy the session history and tell the user what the last run left behind (they would not otherwise know)."""
        try:
            # Listed before the database is read: a launch starting right now already has its row, so its record
            # can never be mistaken for one left behind.
            worker_records = sorted((self.store.path.parent / "sessions").glob("*.json"))
            self._records["sessions"] = self.store.list("sessions")
            self._reap_closed_terminals()
            live = {s["id"] for s in self._records["sessions"] if _value(s, "status") not in {"ended", "failed"}}
            local = {e["id"] for e in self._records.get("environments", []) if e.get("kind") == "local"}
            runner_module.end_stale_records(worker_records, live, local)
            self._prune_local_finished()
            finished = sorted((s for s in self._records["sessions"] if _value(s, "status") in {"ended", "failed"}),
                              key=lambda s: _value(s, "updated_at"), reverse=True)
            for session in finished[self.FINISHED_KEPT:]:
                self.store.delete("sessions", session["id"])
            self.reload()                     # every page, not just Sessions: Home and Projects showed the old states
            counts = self._session_counts()
        except Exception:
            return
        if counts["unknown"] or counts["live"]:
            parts = ([tr('ui.96af3f6c5b84', v0=f"{counts['live']}")] if counts["live"] else []) + ([tr('ui.f48e708320a9', v0=f"{counts['unknown']}")] if counts["unknown"] else [])
            self._toast(tr('ui.d5dfb48c25f8') + "、".join(parts) + tr('ui.43d2538edafa'))

    def _install_shortcuts(self) -> None:
        from . import shortcuts
        for shortcut in getattr(self, "_app_shortcuts", []):
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self._app_shortcuts = []
        def bind(sequence: str, callback: Any) -> None:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(callback)
            self._app_shortcuts.append(shortcut)

        shortcuts.configure(self.prefs.shortcuts)
        self.sidebar._apply_rail(animate=False)
        self.workspace.palette_button.setText(shortcuts.bindings()["palette"])
        for action, sequence in shortcuts.bindings().items():
            if not action.startswith("nav."):
                bind(sequence, lambda a=action: self.dispatch_shortcut(a))
                if action == "zoom.in" and sequence.endswith("+="):
                    # Ctrl and "+" too: on the number pad, or Shift+= (Qt reports that one with Shift held).
                    for alias in (sequence[:-1] + "+", sequence[:-1] + "Shift++"):
                        if QKeySequence(alias) != QKeySequence(sequence):
                            bind(alias, lambda: self.dispatch_shortcut("zoom.in"))

        if getattr(self, "_list_shortcuts_installed", False):
            return
        self._list_shortcuts_installed = True

        # Lists: Enter does the main thing, Delete removes (each still asks to confirm).
        def bind_list(listing: QListWidget, enter: Any, delete: Any) -> None:
            for keys, callback in ((("Return", "Enter"), enter), (("Delete",), delete)):
                for sequence in keys:
                    shortcut = QShortcut(QKeySequence(sequence), listing)
                    shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
                    shortcut.activated.connect(callback)

        bind_list(self.project_list, lambda: self.launch_project(), lambda: self.delete_project())
        bind_list(self.sessions_list, lambda: self.attach_session(), lambda: self.remove_session())

    def dispatch_shortcut(self, action: str) -> None:
        from .navigation import FocusNavigation, cycle_window
        callbacks = {"palette": self.open_palette, "workspace.palette": self.open_palette,
                     "sidebar": self.toggle_sidebar, "search": self.focus_search, "create": self.new_for_page,
                     "settings": self.open_settings}
        if action in callbacks:
            callbacks[action]()
        elif action == "navigate":
            if not hasattr(self, "focus_navigation"):
                self.focus_navigation = FocusNavigation(self)
            self.focus_navigation.start()
        elif action in {"window.next", "window.prev"}:
            cycle_window(self, 1 if action == "window.next" else -1)
        elif action.startswith("page:"):
            self.show_page(self.PAGES[int(action.split(":")[1]) - 1])
        elif action in {"zoom.in", "zoom.out", "zoom.reset"}:
            if self.current_page() == "workspace":
                self.zoom_terminal({"zoom.in": 1, "zoom.out": -1, "zoom.reset": 0}[action])
        elif self.current_page() == "workspace":
            self.workspace._on_chord(action)

    def apply_controls(self) -> None:
        for window in QApplication.topLevelWidgets():
            if isinstance(window, VibeLauncherWindow):
                window.prefs.shortcuts = dict(self.prefs.shortcuts)
                window.prefs.composer_position = self.prefs.composer_position
                window._install_shortcuts()
                window.workspace.composer_position = self.prefs.composer_position
                for tab in window.workspace.tabs():
                    tab.pane.set_composer_position(self.prefs.composer_position)
                    tab.pane.refresh_shortcut_labels()
                    apply = getattr(tab.terminal, "apply_shortcuts", None)
                    if apply:
                        apply()

    def restart_launcher(self) -> bool:
        """Close (asking about running terminals as usual), then start the launcher again. False if the user stays."""
        if not self.close():
            return False
        program, arguments = selfcmd.gui_launch()
        environment = QProcessEnvironment.systemEnvironment()
        if _scale_from_prefs:
            environment.remove("QT_SCALE_FACTOR")      # let the new process read the newly saved scale
        process = QProcess()
        process.setProgram(program)
        process.setArguments(arguments)
        process.setWorkingDirectory(os.getcwd())
        process.setProcessEnvironment(environment)
        started = process.startDetached()
        return bool(started[0] if isinstance(started, tuple) else started)

    def open_settings(self, page: str = "general") -> None:
        from .settings_dialog import SettingsDialog
        try:
            SettingsDialog(self, page if isinstance(page, str) else "general").exec()
        except (OSError, ValueError) as error:
            self._error(tr("Settings"), error)

    def _page_root(self, page: QWidget) -> QVBoxLayout:
        root = QVBoxLayout(page)
        root.setContentsMargins(S.xl, S.xl, S.xl, S.xl)
        root.setSpacing(S.lg)
        return root

    def _button(self, text: str, callback: Any, primary: bool = False, icon: str | None = None) -> Any:
        return make_button(text, "primary" if primary else "secondary", icon, callback=lambda *_: callback())

    # -- projects ------------------------------------------------------------------
    def _build_projects(self) -> QWidget:
        page = QWidget()
        root = self._page_root(page)
        header = PageHeader(tr('ui.e564b916b12e'), tr('ui.e58d44b8fc00'), icon="folder", hue=section_hue("projects"))
        self.search = SearchField(tr('ui.6e33995a3818'))
        self.search.setMinimumWidth(160)
        self.search.setMaximumWidth(300)
        self.search.textChanged.connect(self._render_projects)
        self.sort = QComboBox()
        self.sort.addItem(tr('ui.39a9046f9c8c'), "recent")
        self.sort.addItem(tr('ui.a5274f381305'), "name")
        self.sort.setAccessibleName(tr('ui.a96c9a854190'))
        self.sort.currentIndexChanged.connect(self._render_projects)
        self.favorite_filter = QCheckBox(tr('ui.bff836d78d5a'))
        self.favorite_filter.stateChanged.connect(self._render_projects)
        self.project_workspace_filter = QComboBox()
        self.project_workspace_filter.setAccessibleName(tr("Filter by workspace"))
        self.project_workspace_filter.setMaximumWidth(220)
        self.project_workspace_filter.addItem(tr("All workspaces"), ALL_WORKSPACES)
        self.project_workspace_filter.currentIndexChanged.connect(self._render_projects)
        header.add_action(self.search, 1)
        header.add_action(make_button(tr('ui.4efd9b7b6756'), "primary", "plus", callback=lambda *_: self.add_project()))
        root.addWidget(header)

        self.tag_bar = TagFilterBar()
        self.tag_bar.set_selected(self.prefs.project_tags)
        self.tag_bar.changed.connect(self._project_tags_changed)
        filters = QHBoxLayout()
        filters.setSpacing(S.md)
        filters.addWidget(self.tag_bar, 1)
        filters.addWidget(self.favorite_filter)
        filters.addWidget(self.project_workspace_filter)
        filters.addWidget(self.sort)
        root.addLayout(filters)

        self.project_env_chip = make_button("", "subtle", "x", small=True, tip=tr('ui.0add8022c561'), callback=lambda *_: self._set_project_env_filter(None))
        chip_row = QHBoxLayout()
        chip_row.addWidget(self.project_env_chip)
        chip_row.addStretch(1)
        self.project_chip_row = QWidget()
        self.project_chip_row.setLayout(chip_row)
        chip_row.setContentsMargins(0, 0, 0, 0)
        self.project_chip_row.hide()
        root.addWidget(self.project_chip_row)

        self.project_list = QListWidget()
        self.project_list.currentItemChanged.connect(self._project_selected)
        self.project_list.itemDoubleClicked.connect(lambda _: self.launch_project())
        self.project_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.project_list.customContextMenuRequested.connect(self._project_context_menu)
        self.project_icon_click = ProjectIconClick(self.project_list)
        self.project_icon_click.clicked.connect(self._icon_clicked_in_list)
        empty = EmptyState()
        empty.set_content("folder", tr('ui.c5558838fe05'), tr('ui.b66dd6d9602b'), primary=tr('ui.4efd9b7b6756'))
        empty.primary.clicked.connect(lambda *_: self.clear_project_filters() if self.project_list_filtered else self.add_project())
        self.project_list_filtered = False
        self.project_panel = ListPanel(self.project_list, empty)

        details = QVBoxLayout()
        details.setSpacing(S.md)
        self.project_title = QLabel(tr('ui.44d8ad1a3142'))
        self.project_title.setProperty("role", "dialogTitle")
        self.project_title.setWordWrap(True)
        self.project_info = FactList()
        self.project_info.set_message(tr('ui.471cfce5212b'))
        info_scroll = QScrollArea()
        info_scroll.setWidgetResizable(True)
        info_scroll.setFrameShape(QFrame.Shape.NoFrame)
        info_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.work_drawer = WorkDrawer()
        self.work_drawer.switch_to.connect(self._work_switch)
        self.work_drawer.attach.connect(self._work_attach)
        self.work_drawer.stop.connect(self._work_stop)
        self.work_drawer.remove.connect(self._work_remove)
        self.work_drawer.restart.connect(self._work_restart)
        self.work_drawer.restore_all.connect(lambda: self.restore_project(self._selected_project or ""))
        self.work_drawer.new_work.connect(self.launch_project)
        info_host = QWidget()                       # info + drawer scroll together, so a narrow window stays readable
        info_box = QVBoxLayout(info_host)
        info_box.setContentsMargins(0, 0, S.xs, 0)
        info_box.setSpacing(S.md)
        info_box.addWidget(self.work_drawer)        # the point of this panel when a project is open several times
        facts = QFrame()
        facts.setProperty("surface", "well")
        facts.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        facts_box = QVBoxLayout(facts)
        facts_box.setContentsMargins(S.md + 2, S.md, S.md + 2, S.md)
        facts_box.addWidget(self.project_info)
        info_box.addWidget(facts)
        info_box.addStretch(1)
        info_scroll.setWidget(info_host)
        title_row = QHBoxLayout()
        title_row.setSpacing(S.md)
        self.project_tile = ProjectIconTile(36, 18, radius=R.md)
        self.project_tile.set_icon("folder", section_hue("projects"))
        self.project_tile.clicked.connect(lambda: self.change_project_icon())
        title_row.addWidget(self.project_tile, 0, Qt.AlignmentFlag.AlignTop)
        title_row.addWidget(self.project_title, 1)
        details.addLayout(title_row)
        self.project_workspaces_host = QWidget()           # the desktops this project is in; a click switches to one
        self.project_workspaces = FlowLayout(self.project_workspaces_host, S.xs, S.xs)
        self.project_workspaces.setContentsMargins(0, 0, 0, 0)
        details.addWidget(self.project_workspaces_host)
        details.addWidget(info_scroll, 1)
        actions_host = QWidget()
        actions = FlowLayout(actions_host, S.sm, S.sm)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.addWidget(make_button(tr('ui.0748e3c5fd75'), "primary", "terminal", callback=lambda *_: self.launch_project()))
        self.open_in_workspace_button = make_button(tr("Open in workspace…"), "secondary", WORKSPACE_GLYPH,
                                                    tip=tr("Open this project on the current, another or a new workspace"))
        self.open_in_workspace_button.clicked.connect(
            lambda *_: self._menu(self.open_in_workspace_button, self._open_in_workspace_actions()))
        actions.addWidget(self.open_in_workspace_button)
        actions.addWidget(make_button(tr('ui.9f7cfab95388'), "secondary", "square-terminal", tip=tr('ui.769a81b48ac2'),
                                      callback=lambda *_: self.open_shell()))
        actions.addWidget(make_button(tr('ui.391babf73637'), "secondary", "activity", tip=tr('ui.40e450de4055'),
                                      callback=lambda *_: self.attach_project()))
        self.favorite_button = make_button(tr('ui.60a53514eb92'), "ghost", "star", callback=lambda *_: self.toggle_favorite())
        self.favorite_button.setParent(actions_host)
        self.favorite_button.hide()
        project_more = IconButton("more", tr('ui.537a824da34d'), size=36, icon_size=18)
        project_more.clicked.connect(lambda: self._menu(project_more, [
            MenuAction(tr('ui.3cbe9ed6d2fd'), self.github_settings, "github"),
            MenuAction(tr('ui.0c37b0864834'), self.open_github_repository, "external-link"),
            MenuAction("Harness Web…", self.harness_settings, "terminal", separator_before=True),
            MenuAction(tr('ui.97ec759bda09'), self.doctor_project, "check-circle"),
            MenuAction(tr('ui.61bb06719113'), self.toggle_favorite, "star", separator_before=True),
            MenuAction(tr('ui.6a51e329699a'), self.edit_project, "pencil"),
            MenuAction(tr("Change icon…"), self.change_project_icon, "palette"),
            MenuAction(tr('ui.9b9fe0d469be'), self.delete_project, "trash", danger=True)]))
        actions.addWidget(project_more)
        details.addWidget(actions_host)
        root.addWidget(AdaptiveSplit(self.project_panel, _surface(details), 3, 2), 1)
        return page

    # -- accounts / sessions ---------------------------------------------------------------
    def _build_accounts(self) -> QWidget:
        page = self.accounts_page = AccountsPage()
        page.add_requested.connect(self._add_account_tool)
        page.selected.connect(lambda *_: self._refresh_account_details())
        page.action_requested.connect(self._account_action)
        self.account_details = QLabel()
        self.account_details.setWordWrap(True)
        page.add_detail_widget(self.account_details)
        self.quota_panel = QuotaPanel()
        self.quota_panel.query_requested.connect(self.query_quota)
        self.quota_panel.environment.currentIndexChanged.connect(lambda *_: self._refresh_quota_panel())
        self._quota_done.connect(self._on_quota_done)
        page.add_detail_widget(self.quota_panel)
        controls_host = QWidget()
        controls = FlowLayout(controls_host, S.sm, S.sm)
        controls.setContentsMargins(0, 0, 0, 0)
        self.account_buttons = {}
        self.account_button_icons = {}
        page.context_requested = self._account_context_menu
        for action, title, icon in (("login", tr('ui.aad60e6f37b3'), "key"), ("edit", tr('ui.e0d4485966bd'), "pencil"),
                                     ("status", tr('ui.5d8a776a2a79'), "refresh"), ("settings", tr('ui.f1b074e234fd'), "folder"),
                                     ("history", tr('ui.4f69b689ae8d'), "folder"), ("delete", tr('ui.8aa57065497d'), "trash")):
            button = make_button(title, "primary" if action == "login" else "secondary", icon,
                                 callback=lambda *_, action=action: page.request_action(action))
            self.account_buttons[action] = button
            self.account_button_icons[action] = icon
            controls.addWidget(button)
        page.add_detail_widget(controls_host)
        return page

    def _build_sessions(self) -> QWidget:
        page = QWidget()
        root = self._page_root(page)
        header = PageHeader(tr('ui.eda135194fbc'), tr('ui.2210fc36fc73'),
                            icon="activity", hue=section_hue("sessions"))
        header.add_action(make_button(tr('ui.167c2536cd42'), "secondary", "refresh", callback=lambda *_: self.refresh_auth()))
        root.addWidget(header)
        # One toolbar: what to show on the left, what to do with the selection on the right.
        toolbar_host = QWidget()
        toolbar = FlowLayout(toolbar_host, S.sm, S.sm)          # wraps instead of squeezing on a narrow window
        toolbar.setContentsMargins(0, 0, 0, 0)
        self.session_filter = FilterBar([("all", tr("All")), ("live", tr("Active")), ("unknown", tr("Unknown")),
                                         ("finished", tr("Finished"))])
        self.session_filter.changed.connect(lambda *_: self._render_sessions())
        toolbar.addWidget(self.session_filter)
        self.session_grouping = FilterBar([("workspace", tr("By workspace")), ("time", tr("By time"))])
        self.session_grouping.set_current(getattr(self.prefs, "sessions_grouping", "workspace"), emit=False)
        self.session_grouping.changed.connect(self.set_sessions_grouping)
        toolbar.addWidget(self.session_grouping)
        self.session_attach_button = make_button(tr('ui.3c1df1893978'), "primary", "terminal", callback=lambda *_: self.attach_session(),
                                                 tip=tr('ui.b607cd0ebfc5'))
        self.session_stop_button = make_button(tr('ui.5b3fd961aaac'), "danger", "x-circle", callback=lambda *_: self.stop_session(),
                                               tip=tr('ui.1485ca87e130'))
        self.session_remove_button = make_button(tr('ui.3f922a7e67a3'), "ghost", "trash", callback=lambda *_: self.remove_session(),
                                                 tip=tr('ui.4fe9d5dbe1b1'))
        for button in (self.session_attach_button, self.session_stop_button, self.session_remove_button):
            toolbar.addWidget(button)
        root.addWidget(toolbar_host)
        self.session_check_button = make_button(tr('ui.c6008e05f901'), "ghost", "refresh", small=True, callback=lambda *_: self.check_unknown_sessions(),
                                                tip=tr('ui.089edd920c3c'))
        self.session_clean_button = make_button(tr('ui.7c631dac6c1b'), "ghost", "trash", small=True, callback=lambda *_: self.clean_finished_sessions(),
                                                tip=tr('ui.da42f5ae1297'))
        toolbar.addWidget(self.session_check_button)
        toolbar.addWidget(self.session_clean_button)
        self.session_summary = QLabel()
        self.session_summary.setProperty("role", "meta")
        self.session_summary.setWordWrap(True)
        root.addWidget(self.session_summary)
        self.sessions_list = QListWidget()
        self.sessions_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.sessions_list.setAccessibleDescription(tr('ui.b8338668b07e'))
        empty = EmptyState()
        empty.set_content("activity", tr('ui.d30cb8506bc4'), tr('ui.7808c53ba927'), primary=tr('ui.f0e7eb4eb6b2'))
        empty.primary.clicked.connect(lambda *_: self.show_page("projects"))
        self.sessions_panel = ListPanel(self.sessions_list, empty)
        self.session_hint = QLabel()
        self.session_hint.setProperty("role", "muted")
        self.session_hint.setWordWrap(True)
        root.addWidget(self.session_hint)
        root.addWidget(self.sessions_panel, 1)
        self.sessions_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sessions_list.customContextMenuRequested.connect(self._session_context_menu)
        self.sessions_list.currentItemChanged.connect(lambda *_: self._sync_session_actions())
        self.sessions_list.itemSelectionChanged.connect(self._sync_session_actions)
        self.auth_summary = QLabel()
        self.auth_summary.setProperty("role", "meta")
        self.auth_summary.setWordWrap(True)
        root.addWidget(self.auth_summary)
        return page

    # -- workspace -------------------------------------------------------------------------------
    def _build_workspace(self) -> QWidget:
        self._incomplete_workspaces: set[str] = set()
        self._desktops_ready = False      # nothing is saved before last time's desktops are offered back
        self._pending_slots: dict[str, list[dict]] = {}     # last run's terminals not brought back yet, per desktop
        self._known_terminal_workspaces: dict[str, str] = {}
        page = WorkspacePage(terminal_factory=self._terminal_factory)
        page.composer_default = self.prefs.composer_default
        page.composer_position = self.prefs.composer_position
        page.command_requested.connect(self.dispatch_shortcut)
        page.terminal_requested.connect(lambda preset, profile: self.open_terminal(preset, "shell", terminal_profile=profile))
        page.composer_position_changed.connect(self._remember_composer_position)
        from .settings import profiles
        page.terminal_profiles = lambda preset: profiles(self.store.path.parent, "windows" if
            self._by_id("environments", self._by_id("presets", preset).get("environment_id", "local")).get("kind") == "local"
            and sys.platform == "win32" else "linux")
        page.draft_root = self.store.path.parent
        self.workspace = page
        page.set_layout(self.prefs.workspace_layout)
        page.launch_requested.connect(self.open_terminal)
        page.history_refresh_requested.connect(self.refresh_project_history)
        page.quota_policy_requested.connect(self.edit_terminal_quota)
        page.account_switch_requested.connect(self.switch_terminal_account)
        page.palette_requested.connect(self.open_palette)
        page.notify.connect(lambda message, tone: self._toast(message, tone))
        page.save_workspace_requested.connect(self.save_workspace)
        page.new_workspace_requested.connect(self.new_workspace)
        page.edit_workspace_requested.connect(self.edit_workspace)
        page.restore_pending_requested.connect(self.restore_pending)
        page.discard_pending_requested.connect(self.discard_pending)
        page.reload_requested.connect(self.reload_terminal)
        page.detach_requested.connect(self.detach_terminal)
        page.prefs = self.prefs
        page.open_workspace_requested.connect(self.open_workspace)
        page.delete_workspace_requested.connect(self.delete_workspace)
        page.layout_changed.connect(self._remember_layout)
        page.terminals_changed.connect(self._on_terminals_changed)
        page.terminals_changed.connect(self._sync_quota_controls)
        page.restore_requested.connect(self.restore_project)
        page.launcher.set_tag_filter(self.prefs.workspace_tags)
        page.launcher.tags_changed.connect(self._workspace_tags_changed)
        return page

    def _remember_layout(self, mode: str) -> None:
        self.prefs.workspace_layout = mode
        self.prefs.save()
        self._remember_workspaces()

    def _on_terminals_changed(self) -> None:
        """A terminal opened, closed or moved: the desktops are saved as they are now, and membership, the drawer and
        the Home cards follow."""
        tabs = self.workspace.tabs()
        new_tabs = [tab for tab in tabs if self._known_terminal_workspaces.get(tab.info.id) != tab.info.workspace_id]
        self._known_terminal_workspaces = {tab.info.id: tab.info.workspace_id for tab in tabs}
        for tab in new_tabs:
            info = tab.info
            if not info.preset_id or info.kind not in {"agent", "shell", "attach"}:
                continue
            pending = self._pending_slots.get(info.workspace_id, [])
            matches = [i for i, slot in enumerate(pending) if self._slot_matches_tab(slot, tab)]
            if matches:
                # Consume once, when a tab opens. Saving/layout changes must not
                # consume additional saved terminals of the same project.
                index = next((i for i in matches if pending[i].get("session_id") == info.session_id), matches[0])
                pending.pop(index)
                self.workspace.set_pending(info.workspace_id, len(pending))
        self._remember_workspaces()
        self._prune_local_finished()
        self._render_projects()
        self._refresh_project_drawer()
        self._render_home()

    # -- desktops are saved as you go, so a crash or a forced close loses nothing ---------------------------------
    def _remember_workspaces(self) -> None:
        if (not hasattr(self, "workspace") or not self._desktops_ready or self.workspace.shutting_down or getattr(self, "_closing", False)):
            return
        page = self.workspace
        prefs_changed = False
        for workspace_id in page.desktop_ids():
            if not page.has_workspace(workspace_id) or workspace_id in self._incomplete_workspaces:
                continue
            slots = self._unique_slots(page.slots(workspace_id) + self._pending_slots.get(workspace_id, []))[:16]
            layout = page.desktop_layout(workspace_id)
            if not workspace_id:
                desktop = {"layout": layout, "slots": slots}
                if desktop != self.prefs.default_workspace:
                    self.prefs.default_workspace, prefs_changed = desktop, True
                continue
            record = self._by_id("workspaces", workspace_id)
            if record and {**record, "layout": layout, "slots": slots} != record:
                try:
                    saved = self.store.save("workspaces", {**record, "layout": layout, "slots": slots})
                except Exception as exc:
                    self._error(tr('ui.12022293daa0'), exc)
                    continue
                self._records["workspaces"] = [saved if w.get("id") == workspace_id else w
                                               for w in self._records.get("workspaces", [])]
        last = page.workspace_id if self._by_id("workspaces", page.workspace_id) else ""
        if last != self.prefs.last_workspace:
            self.prefs.last_workspace, prefs_changed = last, True
        if prefs_changed:
            self.prefs.save()

    def _prune_local_finished(self) -> None:
        """Stopped local runs leave no record behind: the workspace remembers what to reopen, and nothing local can be
        re-attached. A record whose terminal is still on screen stays until that terminal is closed."""
        on_screen = {tab.info.session_id for tab in self.workspace.tabs()} if hasattr(self, "workspace") else set()
        local = {e.get("id") for e in self._records.get("environments", []) if e.get("kind") == "local"}
        gone = [s["id"] for s in self._records.get("sessions", []) if s.get("environment_id") in local
                and s.get("status") in {"ended", "failed"} and s["id"] not in on_screen]
        for session_id in gone:
            try:
                self.store.delete("sessions", session_id)
                record = runtime_module.session_path(self.store.path.parent, session_id)
                runtime_module.assert_private_path(record)
                record.unlink(missing_ok=True)      # the worker's own copy goes too
            except Exception:
                continue
        if gone:
            self._records["sessions"] = self.store.list("sessions")

    def _effective_sessions(self) -> list[dict[str, Any]]:
        """Session records as the workspaces see them, so a project's "running" matches the open terminals: a session
        runs while its terminal is open; on a server it may go on in tmux (background); local work without an open
        terminal is over, whatever the last record said."""
        sessions = self._records.get("sessions", [])
        if not hasattr(self, "workspace"):
            return sessions
        alive = {tab.info.session_id for tab in self.workspace.tabs() if tab.info.session_id and tab.alive()}
        local = {e.get("id") for e in self._records.get("environments", []) if e.get("kind") == "local"}
        embedded = self._embedded_enabled()      # separate console windows are not tracked as tabs
        result = []
        for session in sessions:
            status = session.get("status")
            if session.get("id") in alive:
                status = status if status in {"queued", "connecting", "waiting_auth"} else "running"
            elif status in env_state.LIVE_STATUSES:
                if session.get("environment_id") in local:
                    status = "ended" if embedded else status
                elif status == "running":
                    # tmux keeps it on the server; run directly (no tmux) it ended with its terminal, as far as we know
                    status = "detached" if session.get("persistent", True) is not False else "unknown"
            result.append(session if status == session.get("status") else {**session, "status": status})
        return result

    def _restorable_projects(self) -> set[str]:
        """Projects a workspace can bring back as they were (last run's terminals, or a saved desktop not opened yet)."""
        if not hasattr(self, "workspace"):
            return set()
        page = self.workspace
        found = {slot.get("preset_id", "") for slots in self._pending_slots.values() for slot in slots}
        for record in self._records.get("workspaces", []):
            if not page.has_workspace(record.get("id", "")):
                found |= {slot.get("preset_id", "") for slot in record.get("slots", [])}
        return found - {tab.info.preset_id for tab in page.tabs() if tab.alive()} - {""}

    def _project_badge(self, preset_id: str, effective: list[dict[str, Any]], restorable: set[str]) -> tuple[str, str] | None:
        badge = env_state.project_badge(preset_id, effective)
        if preset_id in restorable and env_state.project_status(preset_id, effective) not in env_state.LIVE_STATUSES:
            return tr("Restorable"), "info"
        return badge

    def _restore_last_desktop(self) -> None:
        """At start: show the desktop used last time and offer its terminals (nothing starts by itself)."""
        page = self.workspace
        default = self.prefs.default_workspace
        if default.get("slots") and default.get("layout", "tabs") != page.area.mode:
            page.set_layout(default["layout"])
        # The default desktop is always loaded, so its saved terminals wait here whatever desktop is shown; otherwise the
        # first save would record it as empty and lose them. Separate-window mode keeps them too, only without the button.
        pending = {"": self._unique_slots(default.get("slots", []))}
        record = self._by_id("workspaces", self.prefs.last_workspace) if self.prefs.last_workspace else {}
        if record:
            page.switch_workspace(record["id"], record.get("layout", "tabs"))
            pending[record["id"]] = self._unique_slots(record.get("slots", []))
        embedded = self._embedded_enabled()
        for workspace_id, slots in pending.items():
            if slots:
                self._pending_slots[workspace_id] = slots
                page.set_pending(workspace_id, len(slots) if embedded else 0)
        self._desktops_ready = True
        self._render_projects()                 # projects that can come back say so
        self._render_home()

    def restore_pending(self, workspace_id: str) -> None:
        slots = self._pending_slots.pop(workspace_id, [])
        self.workspace.set_pending(workspace_id, 0)
        if workspace_id != self.workspace.workspace_id:
            self.open_workspace(workspace_id)
        self._restore_slots(workspace_id, slots, match_existing=False)
        self._remember_workspaces()

    @staticmethod
    def _unique_slots(slots: list[dict]) -> list[dict]:
        """A conversation or remote session is one terminal: keep its first slot, drop copies (earlier builds saved a
        terminal twice when it was opened by hand before Restore, so each restart added more)."""
        seen: set[str] = set()
        unique = []
        for slot in slots:
            session_id = slot.get("session_id") or ""
            if session_id and session_id in seen:
                continue
            seen.add(session_id)
            unique.append(slot)
        return unique

    @staticmethod
    def _slot_matches_tab(slot: dict, tab) -> bool:
        info = tab.info
        kind = "shell" if info.tool == "shell" else "agent"
        return (slot.get("preset_id") == info.preset_id and slot.get("kind", "agent") == kind
                and (not slot.get("environment_id") or slot["environment_id"] == info.environment_id)
                and (kind == "shell" or not slot.get("account_id") or slot["account_id"] == info.account_id)
                and slot.get("terminal_profile", "") == info.terminal_profile)

    def discard_pending(self, workspace_id: str) -> None:
        slots = self._pending_slots.get(workspace_id, [])
        if not slots:
            return
        names = "\n".join(self._by_id("presets", slot.get("preset_id", "")).get("name", tr("Deleted project"))
                          for slot in slots)
        message = tr("Discard {count} saved terminal(s) from this workspace? Running work and conversation history are kept.",
                     count=len(slots)) + "\n\n" + names
        if not self._confirm(tr("Discard saved terminals"), message, confirm_text=tr("Discard"), danger=True):
            return
        self._pending_slots.pop(workspace_id, None)
        self.workspace.set_pending(workspace_id, 0)
        self._remember_workspaces()
        self._render_projects()
        self._render_home()

    def reload_terminal(self, tab_id: str) -> None:
        """Start a terminal again in place: tmux work is re-attached, anything else restarts (agents resume)."""
        page = self.workspace
        tab = page.tab(tab_id)
        if tab is None or not tab.info.preset_id:
            return
        info = tab.info
        order = [t.info.id for t in page.current_tabs()]
        position = order.index(tab_id) if tab_id in order else len(order)
        before = set(order)
        # A tmux session the CLI itself ended (e.g. Ctrl+C) is gone: start the conversation again instead of attaching.
        known = str(getattr(page, "_sessions", {}).get(info.session_id, {}).get("status", ""))
        reattach = bool(info.tmux and info.session_id) and (tab.alive() or known not in {"ended", "failed"})
        dropped = []

        def drop_old() -> None:
            if dropped:
                return
            dropped.append(True)
            tab.closing = "close"
            if tab.alive():
                tab.terminal.terminate()
            page._remove(tab_id)
            if info.local and info.session_id:      # its work ended with it; do not leave "queued"/"running" behind
                self._end_local_sessions([info.session_id])

        if reattach:
            drop_old()                              # attaching to a session that is still shown would just switch to it
            self._invoke("attach", session_id=info.session_id)
        elif info.tool == "shell":
            self.open_terminal(info.preset_id, "shell", "", "", info.terminal_profile)
        else:
            managed = info.tool in {"codex", "claude"}
            options = self._quota_launch_options(info.session_id, info.environment_id) if managed else {}
            if managed and info.resume_thread_id:
                options.setdefault("resume_thread_id", info.resume_thread_id)
            if options.get("resume_thread_id"):
                drop_old()                          # one writer per conversation: the old one ends before the new starts
            self.open_terminal(info.preset_id, "agent", options.pop("account_id", "") or info.account_id, "", resume=True,
                               **options)
        added = [t.info.id for t in page.current_tabs() if t.info.id not in before and t.info.id != tab_id]
        if not added:
            if reattach:                            # keep it in the desktop: Restore tries again
                shell = info.tool == "shell"
                slot = {"preset_id": info.preset_id, "kind": "shell" if shell else "agent",
                        **({"account_id": info.account_id} if info.account_id and not shell else {})}
                self._pending_slots[page.workspace_id] = self._pending_slots.get(page.workspace_id, []) + [slot]
                page.set_pending(page.workspace_id, len(self._pending_slots[page.workspace_id]))
                self._remember_workspaces()
            self._notice(tr("The terminal could not be started again; the old one was kept where possible."))
            return
        if not reattach:
            drop_old()                              # only once the new terminal is there
        page.place_tab(added[0], position)
        if not reattach and info.tool in {"codex", "claude"}:
            self._carry_quota_policy(info.session_id, page.tab(added[0]).info.session_id)

    # -- which projects each desktop holds ----------------------------------------------------------------
    def _desktop_projects(self) -> dict[str, set[str]]:
        """A loaded desktop holds what it would save right now; one not loaded yet (or only partly restored) its saved slots."""
        page = self.workspace
        result: dict[str, set[str]] = {}
        for workspace_id in page.desktop_ids():
            projects = {slot["preset_id"] for slot in page.slots(workspace_id) + self._pending_slots.get(workspace_id, [])}
            if not page.has_workspace(workspace_id) or workspace_id in self._incomplete_workspaces:
                record = self._by_id("workspaces", workspace_id) if workspace_id else {}
                projects |= {slot.get("preset_id", "") for slot in record.get("slots", [])}
            result[workspace_id] = {p for p in projects if p}
        return result

    def _workspace_membership(self) -> dict[str, list[str]]:
        """Project id -> the desktops it is in, in picker order."""
        members: dict[str, list[str]] = {}
        for workspace_id, projects in self._desktop_projects().items():
            for preset_id in projects:
                members.setdefault(preset_id, []).append(workspace_id)
        return members

    def _workspace_cards(self) -> list[dict[str, Any]]:
        page = self.workspace
        projects = self._desktop_projects()
        cards = []
        for workspace_id in page.desktop_ids():
            tabs = [tab for tab in page.desktop_tabs(workspace_id) if tab.info.kind in {"agent", "shell", "attach"}]
            cards.append({"id": workspace_id, "name": page.workspace_name(workspace_id), "icon": page.icon_entry(workspace_id),
                          "projects": len(projects.get(workspace_id, ())), "open": len(tabs),
                          "running": sum(1 for tab in tabs if tab.alive()), "current": workspace_id == page.workspace_id,
                          "editable": not workspace_id or bool(self._by_id("workspaces", workspace_id))})
        return cards

    def _apply_icon_choice(self, icons_by_key: dict[str, dict], key: str, old: dict | None, entry: dict | None) -> None:
        """Keep ``entry`` as ``key``'s icon (None = the default) and delete the copied picture it replaces."""
        if entry is None:
            icons_by_key.pop(key, None)
        else:
            icons_by_key[key] = entry
        if old and old["kind"] == "file" and old != entry and (entry or {}).get("name") != old["name"]:
            project_icons.remove_picture(self.store.path.parent, old["name"])

    def _remember_composer_position(self, position: str) -> None:
        self.prefs.composer_position = position
        self.prefs.save()
        self.apply_controls()

    def _embedded_enabled(self) -> bool:
        """Use the built-in terminal when the service supports it, it is not switched off, and this machine can host it."""
        if not getattr(self.service, "supports_spawn", False) or self.prefs.external_terminal:
            return False
        return self._terminal_factory is not None or terminal_module.available()

    # ------------------------------------------------------------------ appearance
    def appearance_settings(self) -> dict[str, Any]:
        p = self.prefs
        return {"theme": p.theme, "accent": p.accent, "terminal_font_size": p.terminal_font_size,
                "ui_font": p.ui_font, "terminal_font": p.terminal_font,
                "cursor_blink": p.cursor_blink, "composer_default": p.composer_default}

    def apply_appearance(self, settings: dict[str, Any], save: bool = True) -> None:
        """Switch theme / accent / terminal preferences live: every page, open dialog and terminal follows."""
        p = self.prefs
        p.theme = settings.get("theme", p.theme) if settings.get("theme", p.theme) in theme.THEMES else p.theme
        p.accent = theme.valid_color(settings.get("accent", p.accent))
        size = settings.get("terminal_font_size", p.terminal_font_size)
        p.terminal_font_size = size if size in theme.TERMINAL_FONT_SIZES else p.terminal_font_size
        p.cursor_blink = bool(settings.get("cursor_blink", p.cursor_blink))
        p.composer_default = bool(settings.get("composer_default", p.composer_default))
        p.ui_font = theme.valid_font_family(settings.get("ui_font", p.ui_font))
        p.terminal_font = theme.valid_font_family(settings.get("terminal_font", p.terminal_font))
        before = (theme.families(), theme.families(mono=True))
        theme.set_fonts(p.ui_font, p.terminal_font)
        theme.apply_theme(p.theme, p.accent)
        self.setStyleSheet(theme.build_stylesheet())
        theme.apply_dark_titlebar(self)
        retheme_widgets()
        apply_fonts(before)
        self._apply_window_icon()
        terminal_module.retheme_all()
        terminal_module.configure(font_size=p.terminal_font_size, cursor_blink=p.cursor_blink)
        self.workspace.composer_default = p.composer_default
        theme.theme_bus().changed.emit()
        if save:
            p.save()

    def zoom_terminal(self, step: int) -> None:
        """Make every terminal's text one step larger / smaller (0 = default size) and remember it.

        Only the terminals change, so this skips the full restyle that ``apply_appearance`` does."""
        sizes = theme.TERMINAL_FONT_SIZES
        current = self.prefs.terminal_font_size
        size = 14 if step == 0 else min(max(current + step, sizes.start), sizes.stop - 1)
        terminal_module.configure(font_size=size, announce=True)
        if size != current:
            self.prefs.terminal_font_size = size
            self.prefs.save()

    def open_appearance(self) -> None:
        kept = AppearanceDialog.ask(self, self.appearance_settings(), lambda s: self.apply_appearance(s, save=False))
        self.apply_appearance(kept if kept is not None else self.appearance_settings(), save=kept is not None)

    def set_theme(self, theme_id: str) -> None:
        self.apply_appearance({"theme": theme_id})
        self._toast(tr('ui.d1ae83440661', v0=f'{theme.THEMES[theme_id].name}'))

    def toggle_external_terminal(self) -> None:
        self.prefs.external_terminal = not self.prefs.external_terminal
        self.prefs.save()
        self._toast(tr('ui.d5c27b678f48') if self.prefs.external_terminal else tr('ui.e36caf8303b0'))

    def open_terminal(self, preset_id: str, kind: str = "agent", account_id: str = "", environment_id: str = "", terminal_profile: str = "",
                      resume: bool = False, resume_thread_id: str = "", quota_policy: dict | None = None,
                      quota_cursor: str = "") -> None:
        """Open a project in the workspace: its agent (Codex/Claude) or just an SSH shell, optionally on another account/host."""
        if not self._by_id("presets", preset_id):
            self._notice(tr('ui.ac9411e42933'))
            return
        preset = self._by_id("presets", preset_id)
        kwargs: dict[str, Any] = {"preset_id": preset_id}
        if kind == "shell":
            kwargs["mode"] = "shell"        # SSH only: no account involved
            if terminal_profile:
                kwargs["terminal_profile"] = terminal_profile
        else:
            if not account_id:
                account_id = self._pick_account(preset, environment_id or _value(preset, "environment_id", "local"))
                if not account_id:
                    return
            kwargs["account_id"] = account_id
            if resume:
                kwargs["resume"] = True      # restoring: continue the last conversation in this folder
            if resume_thread_id:
                kwargs["resume_thread_id"] = resume_thread_id      # the exact conversation, never "the latest"
            if quota_policy:
                kwargs["quota_policy"] = quota_policy              # the worker starts with the terminal's saved flow
                if quota_cursor:
                    kwargs["quota_cursor"] = quota_cursor          # ... and where its rotation stood
        if environment_id:
            kwargs["environment_id"] = environment_id
        self._invoke("launch", **kwargs)

    def _eligible_accounts(self, environment_id: str, tool: str = "") -> list[dict[str, Any]]:
        """Accounts that can be used on this environment: API/endpoint ones anywhere, a subscription only where it is signed in."""
        accounts = [a for a in self._records.get("accounts", []) if a.get("tool") in {"codex", "claude"} and runtime_module.account_fits(a, environment_id)
                    and (not tool or a.get("tool") == tool)]
        return sorted(accounts, key=lambda a: (_value(a, "tool"), _value(a, "name").casefold()))

    def _account_detail(self, account: dict[str, Any], environment_id: str) -> str:
        if account.get("auth") == "api":
            return "API / Endpoint" + (f" · {account['model']}" if account.get("model") else "")
        state = next((s for s in self._records.get("auth_states", []) if s.get("id") == f"{account['id']}:{environment_id}"), None)
        signed = tr('ui.c42a66e27201') if state and state.get("status") == "authenticated" else tr('ui.d23921e327c2')
        return tr('ui.ec40a5d6752a', v0=f'{signed}')

    def _ask_account(self, preset: dict[str, Any], environment_name: str, options: list[dict[str, Any]], selected: str) -> tuple[str, bool] | None:
        dialog = AccountChoiceDialog(_value(preset, "name", tr('ui.e564b916b12e')), environment_name, options, selected, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.chosen()

    def _pick_account(self, preset: dict[str, Any], environment_id: str) -> str:
        """The account to open ``preset`` with on ``environment_id``: remembered one, the only one, or the user's choice."""
        eligible = self._eligible_accounts(environment_id)
        if not eligible:
            self._notice(tr('ui.324a02b6096c'))
            return ""
        remembered = _value(preset, "account_id")
        if preset.get("ask_account", True) is False and any(a["id"] == remembered for a in eligible):
            return remembered
        if len(eligible) == 1:
            return eligible[0]["id"]
        environment = self._by_id("environments", environment_id)
        options = [{"id": a["id"], "name": _value(a, "name", tr('ui.67bee6acfc3c')), "tool": a.get("tool", ""), "detail": self._account_detail(a, environment_id)}
                   for a in eligible]
        answer = self._ask_account(preset, _value(environment, "name", environment_id), options, remembered)
        if answer is None:
            return ""
        chosen, skip = answer
        if skip and chosen and preset.get("id"):
            self._save("presets", {**preset, "account_id": chosen, "ask_account": False})
        return chosen

    def open_shell(self) -> None:
        if not self._selected_project:
            return
        self.open_terminal(self._selected_project, "shell")

    def save_workspace(self) -> None:
        slots = self.workspace.slots()
        current = self._by_id("workspaces", self.workspace.workspace_id)
        dialog = RecordDialog(tr('ui.b26ff42b85a4'), [("name", tr('ui.0d9c97256020'), tr('ui.332d60c4d9e3'))], {"name": current.get("name", "")}, None, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.values()["name"].strip()
        if not name:
            self._notice(tr('ui.067e3e6ad298'))
            return
        existing = next((w for w in self._records.get("workspaces", []) if w.get("name") == name), None)
        if existing and existing["id"] != self.workspace.workspace_id:
            self._notice(tr("A workspace with this name already exists. Choose another name."))
            return
        record = {"id": current.get("id") or self._new_id().replace("-", ""), "name": name,
                  "layout": self.workspace.area.mode, "slots": slots}
        if not self._save("workspaces", record):
            return
        if self.workspace.workspace_id != record["id"]:
            old_id = self.workspace.workspace_id
            self.workspace.name_workspace(record["id"])
            if old_id in self._pending_slots:           # last time's terminals not brought back yet move along
                self._pending_slots[record["id"]] = self._pending_slots.pop(old_id)
                self.workspace.set_pending(old_id, 0)
                self.workspace.set_pending(record["id"], len(self._pending_slots[record["id"]]))
        self._incomplete_workspaces.discard(record["id"])
        self._remember_workspaces()
        self._toast(tr('ui.8c85fc561431', v0=f"{(tr('ui.3055a035f0eb') if existing else tr('ui.dba44ed3f54f'))}", v1=f'{name}', v2=f'{len(slots)}'))

    def new_workspace(self) -> str:
        """Create an empty workspace and switch to it; returns its id ("" when cancelled)."""
        dialog = RecordDialog(tr("New workspace"), [("name", tr('ui.0d9c97256020'), tr('ui.332d60c4d9e3'))], {"name": ""}, None, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return ""
        name = dialog.values()["name"].strip()
        if not name:
            self._notice(tr('ui.067e3e6ad298'))
            return ""
        if any(w.get("name") == name for w in self._records.get("workspaces", [])):
            self._notice(tr("A workspace with this name already exists. Choose another name."))
            return ""
        record = {"id": self._new_id().replace("-", ""), "name": name, "layout": "tabs", "slots": []}
        if not self._save("workspaces", record):
            return ""
        self.open_workspace(record["id"])
        return record["id"]

    def edit_workspace(self, workspace_id: str) -> None:
        """Rename a workspace and choose its icon. The default desktop keeps both in the view preferences."""
        from .project_icon_dialog import ProjectIconDialog
        record = self._by_id("workspaces", workspace_id) if workspace_id else {}
        if workspace_id and not record:
            return                                  # a desktop whose record was deleted: save it first
        page = self.workspace
        key = page.icon_key(workspace_id)
        old = self.prefs.workspace_icons.get(key)
        taken = {page.workspace_name(other) for other in page.desktop_ids() if other != workspace_id}
        taken |= {_value(w, "name") for w in self._records.get("workspaces", []) if w.get("id") != workspace_id}
        dialog = ProjectIconDialog(self, page.workspace_name(workspace_id), old, title=tr("Workspace name and icon"),
                                   hue=section_hue("workspace"), glyph=WORKSPACE_GLYPH, name_label=tr('ui.0d9c97256020'), taken=taken)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.result_name()
        try:
            entry = dialog.result_entry(self.store.path.parent)
        except (OSError, ValueError) as exc:
            self._error(tr("Workspace name and icon"), exc)
            return
        if record and name != record.get("name") and not self._save("workspaces", {**record, "name": name}):
            return
        if not record:
            self.prefs.default_workspace_name = "" if name == tr("Default workspace") else name
        self._apply_icon_choice(self.prefs.workspace_icons, key, old, entry)
        self.prefs.save()
        self._render_workspace()
        self._render_projects()
        self._render_home()

    def _save_current_workspace(self) -> bool:
        self._remember_workspaces()             # every desktop is kept up to date as terminals open and close
        return True

    def open_workspace(self, workspace_id: str) -> None:
        workspace = self._by_id("workspaces", workspace_id)
        if not workspace and workspace_id and not self.workspace.has_workspace(workspace_id):
            return
        if workspace_id == self.workspace.workspace_id:
            self.show_page("workspace")
            return
        if workspace.get("slots") and not self.workspace.has_workspace(workspace_id) and not self._embedded_enabled():
            self._notice(tr("Enable built-in terminals before opening a saved workspace."))
            self.workspace._refresh_workspace_picker()
            return
        if not self._save_current_workspace():
            self.workspace._refresh_workspace_picker()
            return
        loaded = self.workspace.has_workspace(workspace_id)
        self.workspace.switch_workspace(workspace_id, workspace.get("layout", "tabs"))
        self.show_page("workspace")
        self._render_home()                     # the current desktop is marked on Home
        if loaded or not workspace:
            return
        self._restore_slots(workspace_id, workspace.get("slots", []))

    def _restore_slots(self, workspace_id: str, slots: list[dict], *, match_existing: bool = True) -> None:
        """Bring a desktop's saved terminals back: re-attach work still running on a server, start the rest again
        (Codex / Claude continue their last conversation)."""
        slots = self._unique_slots(slots)
        opened = skipped = 0
        failed: list[dict] = []
        used_sessions = self._open_session_ids()
        planned = {}
        reserved = set(used_sessions)
        descriptions = []
        # Pending slots already had newly opened tabs deducted when they opened.
        existing = list(self.workspace.desktop_tabs(workspace_id)) if match_existing else []
        for slot in slots:
            project = self._by_id("presets", slot.get("preset_id", ""))
            kind, account_id = slot.get("kind", "agent"), slot.get("account_id", "")
            match = next((tab for tab in existing if self._slot_matches_tab(slot, tab)), None)
            if not project or match is not None:
                if match is not None:
                    existing.remove(match)
                continue
            step = next((candidate for candidate in self._restore_steps(project["id"])
                         if candidate.session["id"] not in reserved
                         and candidate.session.get("mode", "agent") == kind
                         and candidate.session.get("environment_id") == project.get("environment_id")
                         and (kind == "shell" or candidate.session.get("account_id") == account_id)
                         and candidate.session.get("terminal_profile", "") == slot.get("terminal_profile", "")), None)
            planned[id(slot)] = step
            if step:
                reserved.add(step.session["id"])
            verb = tr("Reconnect") if step and step.action == "attach" else tr("Restart program")
            descriptions.append(f"{verb}: {project['name']}")
        if any(step is None or step.action != "attach" for step in planned.values()):
            if not self._confirm(tr("Restore terminals"), "\n".join(descriptions), confirm_text=tr("Restore")):
                waiting = [slot for slot in slots if id(slot) in planned]
                self._pending_slots[workspace_id] = waiting
                self.workspace.set_pending(workspace_id, len(waiting))
                return
        for slot in slots:
            project = self._by_id("presets", slot.get("preset_id", ""))
            kind = slot.get("kind", "agent")
            if project and id(slot) not in planned:
                continue                    # already open here (opened by hand before restoring): no second copy
            count = len(self.workspace.desktop_tabs(workspace_id))
            if project:
                account_id = slot.get("account_id", "")
                if slot.get("environment_id") and slot.get("environment_id") != project.get("environment_id"):
                    account_id = ""         # saved before projects were tied to one host: open on the project's own, and pick again
                # Reuse a recoverable run after a launcher restart instead of creating another remote job.
                step = planned.get(id(slot))
                if step:
                    used_sessions.add(step.session["id"])
                    self._attach_step(step) if step.action == "attach" else self._relaunch(step.session)
                else:
                    options = self._quota_launch_options(slot.get("session_id", ""), project.get("environment_id", "local")) \
                        if kind == "agent" else {}
                    self.open_terminal(slot["preset_id"], kind, options.pop("account_id", "") or account_id, "",
                                       slot.get("terminal_profile", ""), resume=True, **options)
                    self._carry_quota_policy(slot.get("session_id", ""), getattr(self, "_last_launch_id", None))
                if len(self.workspace.desktop_tabs(workspace_id)) > count:
                    opened += 1
                else:
                    failed.append(slot)
            else:
                skipped += 1                # its project was deleted: dropped from the desktop, not a failure
        if skipped:
            self._toast(tr('ui.8d9308ae0c16', v0=f'{opened}', v1=f'{skipped}'), "warn")
        if failed:
            # Kept waiting (and saved) instead of freezing the whole desktop: Restore tries them again.
            self._pending_slots[workspace_id] = self._pending_slots.get(workspace_id, []) + failed
            self.workspace.set_pending(workspace_id, len(self._pending_slots[workspace_id]))
            self._toast(tr("Some terminals could not be restored. Press Restore to try them again."), "warn")
        if failed or skipped:
            self._remember_workspaces()         # no terminal changed, so record the cleaned-up arrangement now

    def _workspace_servers(self, workspace_id: str) -> dict[str, list[str]]:
        """Server environment id -> this workspace's terminals there that may still run in its tmux session."""
        servers: dict[str, list[str]] = {}
        ssh = {e["id"] for e in self._records.get("environments", []) if e.get("kind") == "ssh"}
        for session in self._records.get("sessions", []):
            if (session.get("workspace_id") == workspace_id and session.get("environment_id") in ssh
                    and session.get("status") not in {"ended", "failed"}):
                servers.setdefault(session["environment_id"], []).append(session["id"])
        return servers

    def delete_workspace(self, workspace_id: str) -> None:
        workspace = self._by_id("workspaces", workspace_id)
        if not workspace:
            return
        servers = self._workspace_servers(workspace_id)
        name = _value(workspace, "name")
        if servers:
            hosts = ", ".join(sorted(_value(self._by_id("environments", env_id), "name") or env_id for env_id in servers))
            message = tr("Delete workspace “{name}”? On {servers} its terminals run in the tmux session {session}. "
                         "After deleting it, the launcher ends that tmux session there, with any work still running in "
                         "it. Projects are not affected.", name=name, servers=hosts, session=runtime_module.tmux_group(workspace_id))
        else:
            message = tr('ui.be9189d66047', v0=f"{name}")
        if self._confirm(tr('ui.60f6d6ddd4c7'), message, confirm_text=tr('ui.3c8f5b363ab3'), danger=True):
            if self._delete("workspaces", workspace_id):
                self._incomplete_workspaces.discard(workspace_id)
                for environment_id, session_ids in servers.items():
                    # one SSH connection per server: stops the terminals and ends the workspace's tmux session
                    self._invoke("stop", session_ids=session_ids[:100], environment_id=environment_id, workspace_id=workspace_id)

    def _render_workspace(self) -> None:
        if hasattr(self, "workspace"):
            self.workspace.set_identity(self.prefs.default_workspace_name, self.prefs.workspace_icons)
            self.workspace.set_data(self._records.get("presets", []), self._records.get("accounts", []),
                                    self._records.get("environments", []), self._records.get("workspaces", []))
            self.workspace.update_sessions(self._records.get("sessions", []))

    # -- environments --------------------------------------------------------------------------
    def _build_environments(self) -> QWidget:
        page = EnvironmentsPage()
        self.env_page = page
        self.environments_list = page.environments_list
        self.tool_versions_label = page.detail.tool_versions_label
        self.security_status_label = page.detail.security_status_label
        page.add_requested.connect(lambda: self.add_record("environments"))
        page.palette_requested.connect(self.open_palette)
        page.primary_requested.connect(lambda _id: self.use_environment())
        page.edit_requested.connect(lambda _id: self.edit_record("environments"))
        page.check_requested.connect(lambda _id: self.doctor_environment())
        page.ssh_settings_requested.connect(lambda _id: self.inspect_ssh_settings())
        page.delete_requested.connect(lambda _id: self.delete_record("environments"))
        page.forget_requested.connect(lambda _id: self.forget_ssh_password())
        page.lock_requested.connect(lambda _id: self.lock_ssh_password())
        page.pin_toggled.connect(self.toggle_pin)
        page.check_tools_requested.connect(lambda _id: self.check_tool_versions())
        page.update_tool_requested.connect(lambda _id, tool: self.update_tool(tool))
        return page

    # ------------------------------------------------------------------ chrome behaviour
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_mode()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        theme.apply_dark_titlebar(self)

    def _apply_mode(self) -> None:
        """>=1200 full sidebar + split; 1000-1199 narrower sidebar; <1000 rail; <800 detail becomes a drawer."""
        if not hasattr(self, "sidebar"):
            return
        width = self.width()
        self.sidebar.set_forced_rail(width < 1000)
        self.sidebar.set_expanded_width(Size.sidebar_medium if width < 1200 else Size.sidebar_expanded)
        narrow = width < 800
        margin = S.lg if width < 1000 else S.xl
        for page in self._pages.values():
            layout = page.layout()
            if layout is not None and page not in (self.env_page, self.workspace):
                layout.setContentsMargins(margin, margin, margin, margin)
        self.env_page.set_compact(width < 1000)
        self.workspace.set_compact(width < 1000)
        self.env_page.set_layout_mode("drawer" if narrow else "split")

    def _hide_to_tray(self) -> bool:
        """Close-to-tray: hide the window and keep everything running. False when the tray is off or unavailable."""
        if not self.prefs.minimize_to_tray or getattr(self, "_quitting", False) or not tray_module.available():
            return False
        first = getattr(self, "_tray", None) is None
        if first:
            self._tray = tray_module.TrayIcon(self, on_open=self._show_from_tray, on_quit=self._quit_from_tray)
        QApplication.instance().setQuitOnLastWindowClosed(False)
        self._tray.show()
        if first:
            self._tray.showMessage("Vibe Launcher", tr("Still running in the system tray. Click the icon to reopen it."),
                                   self._tray.MessageIcon.Information, 4000)
        self.hide()
        return True

    def _show_from_tray(self) -> None:
        QApplication.instance().setQuitOnLastWindowClosed(True)
        if getattr(self, "_tray", None) is not None:
            self._tray.hide()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self) -> None:
        self._show_from_tray()
        self._quitting = True
        if not self.close():                    # the user kept the terminals open
            self._quitting = False

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._hide_to_tray():
            event.ignore()
            return
        alive = [tab for tab in self.workspace.tabs() if tab.alive()] if hasattr(self, "workspace") else []
        kept = sum(1 for tab in alive if tab.info.tmux)          # survives on its server
        ending = len(alive) - kept                                # local, or SSH without tmux: ends with the terminal
        if kept and ending:
            message = tr('ui.298721c8b2ff', v0=f'{len(alive)}')
        elif ending:
            message = tr("{count} terminal(s) are still running. Closing Vibe Launcher ends the work in them.", count=ending)
        else:
            message = tr("{count} terminal(s) are connected. Their work keeps running on the server in tmux and can be reconnected from Sessions.",
                         count=kept)
        if alive and not getattr(self, "_installing_update", False) and not self._confirm(tr('ui.8e865844985b'), message, confirm_text=tr('ui.c76448f6bfff'), danger=True):
            event.ignore()
            return
        if hasattr(self, "workspace"):
            if not self._save_current_workspace():
                event.ignore()
                return
            local_ids = [tab.info.session_id for tab in alive if tab.info.local and tab.info.session_id]
            self._closing = True                # what is saved now is what reopens next time
            self.workspace.shutdown()
            self._end_local_sessions(local_ids)
        self.tunnels.shutdown()
        terminal_module.release_warm_page()
        super().closeEvent(event)

    def _end_local_sessions(self, session_ids: list[str]) -> None:
        """The terminals just closed took their local work with them: record that instead of leaving "running" behind."""
        for session_id in session_ids:
            try:
                session = self.store.get("sessions", session_id)
                if session.get("status") not in {"ended", "failed"}:
                    self.store.save("sessions", {**session, "status": "ended", "updated_at": runtime_module.now()})
                runner_module.end_local_record(self.store.path.parent, session_id)
            except Exception:
                continue                      # closing the window must never fail on bookkeeping

    def toggle_sidebar(self) -> None:
        if self.width() >= 1000:
            self.sidebar.set_collapsed(not self.sidebar.is_collapsed())

    def _remember_sidebar(self, collapsed: bool) -> None:
        self.prefs.sidebar_collapsed = collapsed
        self.prefs.save()

    def show_page(self, key: str) -> None:
        self.stack.setCurrentIndex(self.PAGES.index(key))
        self.sidebar.set_current(key)

    def current_page(self) -> str:
        return self.PAGES[self.stack.currentIndex()]

    def focus_search(self) -> None:
        page = self.current_page()
        if page == "environments":
            self.env_page.focus_search()
        elif page == "projects":
            self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
            self.search.selectAll()

    def new_for_page(self) -> None:
        page = self.current_page()
        if page == "environments":
            self.add_record("environments")
        elif page == "accounts":
            self.add_record("accounts")
        elif page == "projects":
            self.add_project()

    def _menu(self, anchor: QWidget, actions: list[MenuAction]) -> None:
        menu = ActionMenu(actions, self)
        menu.popup_below(anchor)

    # -- right-click: the same actions as the page's buttons, at the pointer --------------------------------------
    def _context_menu(self, actions: list[MenuAction], where: QPoint) -> None:
        if actions:
            ActionMenu(actions, self).popup_at(where)

    def _account_context_menu(self, kind: str, record_id: str, where: QPoint) -> None:
        self._refresh_account_details()
        page = self.accounts_page
        self._context_menu([MenuAction(button.text(), lambda action=action: page.request_action(action),
                                       self.account_button_icons.get(action, ""), danger=action == "delete",
                                       separator_before=action == "delete")
                            for action, button in self.account_buttons.items() if not button.isHidden()], where)

    def _project_context_menu(self, pos: QPoint) -> None:
        item = self.project_list.itemAt(pos)
        if item is None or not item.data(Qt.ItemDataRole.UserRole):
            return
        self.project_list.setCurrentItem(item)
        where = self.project_list.viewport().mapToGlobal(pos)
        self._context_menu([
            MenuAction(tr('ui.0748e3c5fd75'), self.launch_project, "terminal"),
            MenuAction(tr("Open in workspace…"), lambda: self._context_menu(self._open_in_workspace_actions(), where),
                       WORKSPACE_GLYPH),
            MenuAction(tr('ui.9f7cfab95388'), self.open_shell, "square-terminal"),
            MenuAction(tr('ui.391babf73637'), self.attach_project, "activity"),
            MenuAction(tr('ui.61bb06719113'), self.toggle_favorite, "star", separator_before=True),
            MenuAction(tr('ui.6a51e329699a'), self.edit_project, "pencil"),
            MenuAction(tr("Change icon…"), self.change_project_icon, "palette"),
            MenuAction(tr('ui.9b9fe0d469be'), self.delete_project, "trash", danger=True, separator_before=True)], where)

    def _session_context_menu(self, pos: QPoint) -> None:
        item = self.sessions_list.itemAt(pos)
        if item is None or not item.data(Qt.ItemDataRole.UserRole):
            return
        if not item.isSelected():
            self.sessions_list.setCurrentItem(item)        # right-click inside a multi-selection keeps it
        self._sync_session_actions()
        buttons = ((self.session_attach_button, self.attach_session, "terminal"),
                   (self.session_stop_button, self.stop_session, "x-circle"),
                   (self.session_remove_button, self.remove_session, "trash"))
        self._context_menu([MenuAction(button.text(), callback, icon, enabled=button.isEnabled(),
                                       danger=callback == self.stop_session)
                            for button, callback, icon in buttons], self.sessions_list.viewport().mapToGlobal(pos))

    # -- command palette -----------------------------------------------------------------------
    def open_palette(self) -> None:
        self.palette.set_commands(self._commands())
        self.palette.open_palette()

    def _commands(self) -> list[Command]:
        from .shortcuts import bindings
        keys = bindings()
        env = self._selected_record("environments")
        env_name = _value(env, "name")
        has_env = bool(env)
        has_ssh = has_env and env.get("kind") == "ssh"
        on_env = tr('ui.d8f9e00b0eae', v0=f'{env_name}') if has_env else tr('ui.a9de1dfa6004')
        commands = [
            Command("env-new", tr('ui.101b65445df8'), tr('ui.97f2870ded3c'), "plus", tr('ui.fba18681418f'), lambda: (self.show_page("environments"), self.add_record("environments")), "add create ssh local"),
            Command("env-use", tr('ui.0938f3db15b4'), on_env, "arrow-right", tr('ui.fba18681418f'), self.use_environment, "connect open project", has_env),
            Command("env-check", tr('ui.ee8ec206811f'), on_env, "check-circle", tr('ui.fba18681418f'), self.doctor_environment, "doctor check", has_env),
            Command("env-ssh", tr('ui.7e39d9b99476'), on_env if has_ssh else tr('ui.4fcd6c174623'), "sliders", tr('ui.fba18681418f'), self.inspect_ssh_settings, "ssh config identity proxyjump", has_ssh),
            Command("tools-check", tr('ui.18d81a8fa464'), on_env, "refresh", tr('ui.d380089db672'), self.check_tool_versions, "codex claude version update", has_env),
            Command("tools-codex", tr('ui.3a936a94030e'), on_env, "arrow-up-circle", tr('ui.d380089db672'), lambda: self.update_tool("codex"), "update upgrade", has_env),
            Command("tools-claude", tr('ui.3d5608f48f28'), on_env, "arrow-up-circle", tr('ui.d380089db672'), lambda: self.update_tool("claude"), "update upgrade", has_env),
            Command("project-new", tr('ui.4efd9b7b6756'), tr('ui.2909a4578e9c'), "folder", tr('ui.e564b916b12e'), self.add_project, "create"),
            Command("account-new", tr('ui.fac920aaecbc'), tr('ui.408d75e82cc7'), "user", tr('ui.969396f78f3b'), lambda: (self.show_page("accounts"), self.add_record("accounts")), "create"),
            Command("config-import", tr('ui.f5475897e4a5'), tr('ui.bd6035b13bb2'), "download", tr('ui.0d8619aae051'), self.import_config),
            Command("config-export", tr('ui.9263e6bbcdf7'), tr('ui.496320690a04'), "upload", tr('ui.0d8619aae051'), self.export_config),
            Command("sidebar", tr('ui.7c8bf8e75d55'), keys["sidebar"], "panel-left", tr('ui.55f633268f47'), self.toggle_sidebar),
            Command("settings", tr("Settings"), keys["settings"], "sliders", tr('ui.b70682d027ce'), self.open_settings),
            Command("appearance", tr('ui.50efb6e0c3cb'), tr('ui.a74b3c7caf22'), "palette", tr('ui.b70682d027ce'), self.open_appearance,
                    tr('ui.0519e5e6ea47')),
        ]
        for theme_id, palette in theme.THEMES.items():
            current = theme_id == self.prefs.theme
            commands.append(Command(f"theme-{theme_id}", tr('ui.c62f20f98e00', v0=f'{palette.name}'), tr('ui.470670fdda37') if current else
                                    (tr('ui.dee60aee2501') if not palette.dark else tr('ui.a6b75d068032')), "palette", tr('ui.b70682d027ce'),
                                    lambda i=theme_id: self.set_theme(i), tr('ui.fa52ae9892ea', v0=f'{theme_id}'), not current))
        pages = (("projects", tr('ui.e564b916b12e'), "folder"), ("workspace", tr('ui.1c5ab76e581d'), "square-terminal"), ("accounts", tr('ui.969396f78f3b'), "user"),
                 ("environments", tr('ui.fba18681418f'), "layers"), ("sessions", tr('ui.eda135194fbc'), "activity"))
        for index, (key, label, icon) in enumerate(pages, start=1):
            commands.append(Command(f"go-{key}", tr('ui.71d0223d2073', v0=f'{label}'), keys[f"page:{index}"], icon, tr('ui.fa96079c3b2f'), lambda k=key: self.show_page(k)))
        for workspace_id in self.workspace.desktop_ids():
            entry = self.workspace.icon_entry(workspace_id)
            glyph = entry["name"] if entry and entry["kind"] == "glyph" else WORKSPACE_GLYPH
            commands.append(Command(f"workspace-{workspace_id or 'default'}",
                                    tr("Switch workspace: {name}", name=self.workspace.workspace_name(workspace_id)),
                                    "", glyph, tr('ui.1c5ab76e581d'), lambda i=workspace_id: self.open_workspace(i)))
        commands.append(Command("workspace-new", tr("New workspace"), "", "plus", tr('ui.1c5ab76e581d'), self.new_workspace))
        commands.append(Command("workspace-edit", tr("Edit the current workspace's name and icon…"), "", "pencil",
                                tr('ui.1c5ab76e581d'), lambda: self.edit_workspace(self.workspace.workspace_id)))
        current_ids = [tab.info.id for tab in self.workspace.current_tabs()]
        for tab in self.workspace.tabs():
            number = current_ids.index(tab.info.id) + 1 if tab.info.id in current_ids else 0
            commands.append(Command(f"term-{tab.info.id}", tr('ui.f7f9c06d7c53', v0=f"{tab.info.project or tr('ui.fd201455c92e')}", v1=f'{tab.info.label}'),
                                    f"{tab.info.environment}  {keys[f'goto:{number}']}" if 0 < number < 10 else tab.info.environment,
                                    "square-terminal" if tab.info.tool == "shell" else TOOL_ICONS.get(tab.info.tool, "terminal"), tr('ui.fd201455c92e'),
                                    lambda i=tab.info.id: (self.show_page("workspace"), self.workspace.select(i)), "switch terminal"))
        for project in sorted(self._records.get("presets", []), key=lambda p: _value(p, "name").casefold()):
            name = _value(project, "name")
            commands.append(Command(f"open-shell-{project['id']}", tr('ui.fe350aabcf0b', v0=f'{name}'), tr('ui.c77be7729d8a'), "square-terminal", tr('ui.f290f9167ad0'),
                                    lambda i=project["id"]: self.open_terminal(i, "shell"), "ssh shell terminal"))
            commands.append(Command(f"open-agent-{project['id']}", tr('ui.b0807568e391', v0=f'{name}'), tr('ui.5dddbd0aeb3a'), "terminal", tr('ui.f290f9167ad0'),
                                    lambda i=project["id"]: self.open_terminal(i, "agent"), "codex claude agent"))
        commands.append(Command("layout-tabs", tr('ui.b9c1459c5cb5'), tr('ui.fb603a86ac4f'), "square-terminal", tr('ui.55f633268f47'),
                                lambda: (self.show_page("workspace"), self.workspace.set_layout("tabs"))))
        commands.append(Command("layout-grid", tr('ui.6e1d2cdf524d'), tr('ui.e97927f53ca3'), "grid", tr('ui.55f633268f47'),
                                lambda: (self.show_page("workspace"), self.workspace.set_layout("grid"))))
        commands.append(Command("terminal-mode", tr('ui.5286894ec5fd') if self.prefs.external_terminal else tr('ui.80d94789ade5'),
                                tr('ui.9511ef77936e') if self.prefs.external_terminal else tr('ui.e101c6db718e'), "repeat", tr('ui.55f633268f47'),
                                self.toggle_external_terminal, "external internal window"))
        for state in env_state.sort_states(list(self.env_page._states.values())):
            commands.append(Command(f"switch-{state.id}", tr('ui.aa826c216fe8', v0=f'{state.name}'), state.subtitle, "server" if state.kind == "ssh" else "monitor", tr('ui.9f611dc4dfe6'),
                                    lambda i=state.id: self.goto_environment(i), f"{state.target} {state.kind} switch"))
        return commands

    def goto_environment(self, env_id: str) -> None:
        self.show_page("environments")
        self.env_page.select(env_id)
        self.env_page.environments_list.setFocus()

    # -- toasts / dialogs -------------------------------------------------------------------------
    def _toast(self, message: str, tone: str = "ok") -> None:
        self.toasts.show(message, tone)

    def _confirm(self, title: str, message: str, *, confirm_text: str = tr('ui.dce52f3e1119'), danger: bool = False) -> bool:
        return ConfirmDialog.ask(self, title, message, confirm_text=confirm_text, danger=danger)

    def _notice(self, message: str) -> None:
        self.toasts.show(message, "warn", 8000 if len(message) > 40 else None)

    def _error(self, title: str, exc: Exception) -> None:
        # Avoid displaying exception text that might include command arguments or secrets.
        message = str(exc) if isinstance(exc, ValueError) else tr('ui.c153252e1d9e', v0=f'{type(exc).__name__}')
        self.toasts.show(f"{title}：{message}", "danger")

    # ------------------------------------------------------------------ data
    def reload(self) -> None:
        try:
            self._records = {kind: self.store.list(kind) for kind in ("accounts", "environments", "presets", "sessions", "auth_states", "workspaces", "github_accounts", "web_services")}
            self._prune_project_icons()
            self._prune_workspace_icons()
            self._render_workspace()                # first: the Projects page reads the desktops from it
            self._render_projects()
            self._render_records("accounts")
            self._render_environments()
            self._render_sessions()
            self._refresh_quota_panel()
            self._render_home()
        except Exception as exc:
            self._error(tr('ui.8b7f4822c68e'), exc)

    def _poll(self) -> None:
        # Re-read SQLite state while preserving the currently selected project/session.
        selected = self._selected_project
        try:
            old_sessions = self._records.get("sessions", [])
            old_auth = self._records.get("auth_states", [])
            self._records.update(self.store.list_many("sessions", "auth_states"))
            self._selected_project = selected
            self._reap_closed_terminals()
            if self._records["sessions"] != old_sessions:
                self._render_workspace()
                self._render_sessions()
                self._render_projects()
            if self._records["auth_states"] != old_auth:
                self._render_records("accounts")
            if self._records["sessions"] != old_sessions or self._records["auth_states"] != old_auth:
                self._render_home()
            self._poll_pending()
            self._poll_quota()
            paths = [env_state.tool_report_path(self.store.path.parent, e["id"]) for e in self._records.get("environments", [])]
            paths += [env_state.diagnostics_path(self.store.path.parent, e["id"]) for e in self._records.get("environments", [])]
            signature = tuple((str(p), p.stat().st_mtime_ns if p.exists() else 0) for p in paths)
            if signature != self._report_signature:
                self._report_signature = signature
                self._render_environments()
            self._poll_operations()
            dialog = getattr(self, "_harness_dialog", None)
            if dialog is not None:
                from shiboken6 import isValid
                if isValid(dialog) and dialog._result.get("service_key"):
                    dialog.set_local_status(self.tunnels.status(dialog._result["service_key"]))
            self.env_page.refresh_times()
            self.statusBar().clearMessage()
        except Exception as exc:
            self.statusBar().showMessage(tr('ui.b1238580dc9f'))
            if time.monotonic() - self._last_poll_error >= 30:
                self._last_poll_error = time.monotonic()
                self._diagnose("poll", exc)

    QUEUED_GRACE_SECONDS = 120     # a queued session's runner records itself within moments of starting

    def _reap_closed_terminals(self) -> bool:
        """A terminal window that was closed takes its runner with it, so the status it last wrote can never advance.
        Move such sessions to "unknown" (they may still run remotely): the user can then refresh to find out."""
        changed = False
        now = datetime.now(timezone.utc)
        for session in self._records.get("sessions", []):
            pid = session.get("runner_pid")
            status = session.get("status")
            updated = _value(session, "updated_at")
            queued = status == "queued"
            if queued:
                written = env_state.parse_time(updated)
                if written is not None and (now - written).total_seconds() < self.QUEUED_GRACE_SECONDS:
                    continue                        # its runner may not have recorded itself yet
            local = self._by_id("environments", _value(session, "environment_id")).get("kind") == "local"
            if local and status in {"queued", "connecting", "waiting_auth", "running", "detached", "unknown"}:
                # Nothing keeps a local session alive once its terminal is gone, so there is nothing "unknown" about it.
                if not pid or not runner_module.runner_alive(pid, updated, local=True, queued=queued):
                    self.store.save("sessions", {**session, "status": "ended", "updated_at": runtime_module.now()})
                    runner_module.end_local_record(self.store.path.parent, session["id"])
                    changed = True
                continue
            if ((pid and status in {"connecting", "waiting_auth"}) or queued) and not (
                    pid and runner_module.runner_alive(pid, updated, local=False, queued=queued)):
                self.store.save("sessions", {**session, "status": "unknown", "updated_at": runtime_module.now()})
                changed = True
        if changed:
            self._records["sessions"] = self.store.list("sessions")
        return changed

    def _fill_list(self, listing: QListWidget, rows: list[tuple[str, Any, str]]) -> None:
        """Replace list rows only when they changed, keeping selection and scroll position."""
        rows = [(*row, *([None] * (5 - len(row)))) for row in rows]
        if getattr(listing, "_rows", None) == rows:
            return
        selected = listing.currentItem().data(Qt.ItemDataRole.UserRole) if listing.currentItem() else None
        chosen = {i.data(Qt.ItemDataRole.UserRole) for i in listing.selectedItems()}
        scroll = listing.verticalScrollBar().value()
        listing.blockSignals(True)
        listing.clear()
        for label, record_id, tip, badge, glyph in rows:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, record_id)
            if glyph:
                item.setData(ICON_ROLE, glyph)
            if badge:
                item.setData(BADGE_ROLE, badge)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{label.splitlines()[0]}，{badge[0]}")
            if tip:
                item.setToolTip(tip)
            listing.addItem(item)
            if record_id is not None and record_id == selected:
                listing.setCurrentItem(item)
            if record_id is not None and record_id in chosen:
                item.setSelected(True)
        listing.blockSignals(False)
        listing._rows = rows
        listing.verticalScrollBar().setValue(scroll)

    # -- projects ---------------------------------------------------------------------------------
    def _project_tags_changed(self, tags: list[str]) -> None:
        self.prefs.project_tags = list(tags)
        self.prefs.save()
        self._render_projects()

    def _workspace_tags_changed(self, tags: list[str]) -> None:
        self.prefs.workspace_tags = list(tags)
        self.prefs.save()

    def _set_project_env_filter(self, env_id: str | None) -> None:
        self._project_env_filter = env_id
        self._render_projects()

    def _render_projects(self, *_: Any) -> None:
        if not hasattr(self, "project_list"):
            return
        selected = self._selected_project
        projects = list(self._records.get("presets", []))
        if self.tag_bar.set_tags(collect_tags(projects)):          # a tag nobody uses any more drops out of the filter
            self.prefs.project_tags = self.tag_bar.selected()
            self.prefs.save()
        projects = [p for p in projects if matches_tags(p, self.tag_bar.selected())]
        term = self.search.text().casefold()
        if self.favorite_filter.isChecked():
            projects = [p for p in projects if p.get("favorite")]
        if self._project_env_filter:
            projects = [p for p in projects if p.get("environment_id") == self._project_env_filter]
        projects = [p for p in projects if term in (_value(p, "name") + " " + _value(p, "path")).casefold()]
        members = self._workspace_membership() if hasattr(self, "workspace") else {}
        chosen = self._refresh_workspace_filter()
        if chosen == NO_WORKSPACE:
            projects = [p for p in projects if not members.get(p.get("id", ""))]
        elif chosen != ALL_WORKSPACES:
            projects = [p for p in projects if chosen in members.get(p.get("id", ""), [])]
        if self.sort.currentData() == "name":
            projects.sort(key=lambda p: _value(p, "name").casefold())
        else:
            projects.sort(key=lambda p: _value(p, "last_used"), reverse=True)
        env = self._by_id("environments", self._project_env_filter or "")
        self.project_chip_row.setVisible(bool(self._project_env_filter))
        self.project_env_chip.setText(tr('ui.e72e91ee2841', v0=f"{_value(env, 'name', tr('ui.4d8c1c5b4283'))}"))
        rows = []
        effective = self._effective_sessions()
        restorable = self._restorable_projects()
        for project in projects:
            favorite = bool(project.get("favorite"))
            tags = " ".join("#" + t for t in project_tags(project))
            names = [self.workspace.workspace_name(w) for w in members.get(project.get("id", ""), [])]
            where = tr("Workspaces: {names}", names=join_names(names)) if names else ""
            label = ("★ " if favorite else "☆ ") + (_value(project, "name") or tr('ui.9009ac5a2f2b')) + "\n" + "  ·  ".join(
                filter(None, (tags, where, _value(project, "path"))))
            tip = _value(project, "path") + ("\n" + where if where else "")
            rows.append((label, project.get("id"), tip, self._project_badge(project.get("id", ""), effective, restorable),
                         project_icons.signature(project.get("id", ""))))
        self._fill_list(self.project_list, rows)
        if not self.project_list.currentItem() and self.project_list.count():
            self.project_list.setCurrentRow(0)
        has_any = bool(self._records.get("presets"))
        self.project_list_filtered = has_any and not self.project_list.count()
        if self.project_list_filtered:
            self.project_panel.empty.set_content("search", tr('ui.efff54056571'), tr('ui.b018cca512d9'),
                                                 primary=tr('ui.050605e81eed'))
        else:
            self.project_panel.empty.set_content("folder", tr('ui.c5558838fe05'),
                                                 tr('ui.b66dd6d9602b'), primary=tr('ui.4efd9b7b6756'))
        self.project_panel.show_empty(not self.project_list.count())
        if not self.project_list.count():
            self._selected_project = None
            self.project_tile.set_icon("folder", section_hue("projects"))
            self.work_drawer.hide()
            self.project_workspaces_host.hide()
            self.project_title.setText(tr('ui.c5558838fe05') if not has_any else tr('ui.efff54056571'))
            self.project_info.setText(tr('ui.8b8558fc54b0') if not has_any
                                      else tr('ui.3ab1e0fbb2db'))
        elif self.project_list.currentItem():
            self._project_selected(self.project_list.currentItem())

    def clear_project_filters(self) -> None:
        """Show every project again: search, favourites, environment and tags."""
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.favorite_filter.blockSignals(True)
        self.favorite_filter.setChecked(False)
        self.favorite_filter.blockSignals(False)
        self._project_env_filter = None
        self.project_workspace_filter.blockSignals(True)
        self.project_workspace_filter.setCurrentIndex(0)
        self.project_workspace_filter.blockSignals(False)
        self.tag_bar.set_selected([])
        self.prefs.project_tags = []
        self.prefs.save()
        self._render_projects()

    def _refresh_workspace_filter(self) -> str:
        """Offer every desktop in the Projects filter, keeping the chosen one while it exists; returns the choice."""
        box = self.project_workspace_filter
        chosen = box.currentData()
        if not hasattr(self, "workspace"):
            return ALL_WORKSPACES
        page = self.workspace
        choices = [(page.workspace_name(w), w, repr(page.icon_entry(w))) for w in page.desktop_ids()]
        if choices != getattr(box, "_choices", None):
            box.blockSignals(True)
            box.clear()
            box.addItem(tr("All workspaces"), ALL_WORKSPACES)
            for name, workspace_id, _ in choices:
                box.addItem(page.workspace_icon(workspace_id), name, workspace_id)
            box.addItem(tr("Not in any workspace"), NO_WORKSPACE)
            index = box.findData(chosen) if chosen is not None else -1
            box.setCurrentIndex(index if index >= 0 else 0)
            box.blockSignals(False)
            box._choices = choices
        return box.currentData()

    def _show_project_workspaces(self, preset_id: str) -> None:
        """Chips of the desktops this project is in (a click switches there), or a note that it is in none."""
        ids = self._workspace_membership().get(preset_id, []) if hasattr(self, "workspace") else []
        signature = [(w, self.workspace.workspace_name(w), repr(self.workspace.icon_entry(w))) for w in ids]
        if signature == getattr(self, "_project_workspaces_shown", None):
            return
        self._project_workspaces_shown = signature
        while self.project_workspaces.count():
            item = self.project_workspaces.takeAt(0)
            if item is not None and item.widget() is not None:
                dispose_widget(item.widget())
        if not ids:
            self.project_workspaces.addWidget(text_label(tr("Not in any workspace"), "meta"))
            return
        self.project_workspaces.addWidget(text_label(tr("Workspaces"), "meta"))
        for workspace_id, name, _ in signature:
            chip = make_button(name, "subtle", small=True, tip=tr("Switch to this workspace"))
            chip.setIcon(self.workspace.workspace_icon(workspace_id))
            chip.setIconSize(QSize(18, 18))
            chip.setProperty("workspaceChip", workspace_id)
            chip.clicked.connect(lambda *_, w=workspace_id: self.open_workspace(w))
            self.project_workspaces.addWidget(chip)

    def _open_in_workspace_actions(self) -> list[MenuAction]:
        page = self.workspace
        actions = []
        for workspace_id in page.desktop_ids():
            entry = page.icon_entry(workspace_id)
            glyph = entry["name"] if entry and entry["kind"] == "glyph" else WORKSPACE_GLYPH
            name = page.workspace_name(workspace_id)
            text = tr("{name} (current)", name=name) if workspace_id == page.workspace_id else name
            actions.append(MenuAction(text, lambda w=workspace_id: self.open_project_in_workspace(w), glyph))
        actions.append(MenuAction(tr("New workspace…"), lambda: self.open_project_in_workspace(None), "plus", separator_before=True))
        return actions

    def open_project_in_workspace(self, workspace_id: str | None) -> None:
        """Open the selected project on a chosen desktop (None: a new one). A terminal for it that the switch just
        restored there is reused instead of opening a second one."""
        preset_id = self._selected_project
        if not preset_id:
            return
        if not self._embedded_enabled():
            self._notice(tr("Workspaces hold built-in terminals. Turn off separate terminal windows to use them."))
            return
        page = self.workspace
        if workspace_id is None:
            workspace_id = self.new_workspace()
            if not workspace_id:
                return
        elif workspace_id != page.workspace_id:
            self.open_workspace(workspace_id)
        if page.workspace_id != workspace_id:
            return                                  # the switch was refused (or its save failed): nothing opens elsewhere
        existing = next((tab for tab in page.desktop_tabs(workspace_id) if tab.info.preset_id == preset_id and tab.alive()
                         and tab.info.kind in {"agent", "attach"}), None)
        if existing is not None:
            self.show_page("workspace")
            page.select(existing.info.id)
            return
        self.open_terminal(preset_id)

    def _project_selected(self, current: QListWidgetItem | None, _: QListWidgetItem | None = None) -> None:
        if not current:
            return
        self._selected_project = current.data(Qt.ItemDataRole.UserRole)
        project = next((p for p in self._records.get("presets", []) if p.get("id") == self._selected_project), {})
        account = self._by_id("accounts", _value(project, "account_id"))
        if account and not runtime_module.account_fits(account, _value(project, "environment_id", "local")):
            account = {}
        environment = self._by_id("environments", _value(project, "environment_id"))
        self.project_title.setText(("★ " if project.get("favorite") else "☆ ") + _value(project, "name", tr('ui.9009ac5a2f2b')))
        self.project_tile.set_project(self._selected_project)
        self.favorite_button.setText(tr('ui.dca60869e7d2') if project.get("favorite") else tr('ui.60a53514eb92'))
        self.project_workspaces_host.show()
        self._show_project_workspaces(_value(project, "id"))
        self.open_in_workspace_button.setVisible(self._embedded_enabled())
        self.work_drawer.show()
        self.work_drawer.set_works(self._work_rows(_value(project, "id")), len(self._restore_steps(_value(project, "id"))))
        status = env_state.project_status(_value(project, "id"), self._effective_sessions())
        if environment.get("kind") == "local":
            run_mode = tr('ui.a40bef948264')
        elif project.get("persistent", True) is False:
            run_mode = tr('ui.ef2a8c8eb776')
        else:
            run_mode = tr('ui.59927729ca2c')
        self.project_info.set_rows([
            (tr('ui.e01b94ddd5d5'), env_state.session_badge(status)[0] if status else tr('ui.cbd8e108b265')),
            (tr('ui.4ff43bc10a99'), run_mode),
            (tr('ui.51d564802120'), _value(project, "path", tr('ui.621330591694')), True),
            (tr('ui.9b423a3fa339'), "、".join(project_tags(project)) or tr('ui.da5930e53d54')),
            (tr('ui.969396f78f3b'), (tr('ui.3ca46b24c892', v0=f"{_value(account, 'name')}", v1=f"{_value(account, 'auth')}") if account else "")
             + ("" if account and project.get("ask_account", True) is False else tr('ui.f147647570a5') if not account else tr('ui.a4478bfff679'))),
            (tr('ui.318bc18e562d'), f"{_value(account, 'tool').title()} · {account.get('model') or tr('ui.f7fd1a5822e3')}" if account else tr('ui.f68a2c7b4d4a')),
            (tr('ui.fba18681418f'), _value(environment, "name", tr('ui.621330591694'))),
            (tr('ui.39a9046f9c8c'), _display_time(_value(project, "last_used"))),
            ("#", "GitHub"),
            ("Repository", _value(project, "github_repo") or (tr('ui.fc35b4e96012')
                                                             if _value(project, "github_account_id") else tr('ui.ea5b1406ca9d'))),
            (tr('ui.bc3a0bab5dfe'), _value(self._by_id("github_accounts", _value(project, "github_account_id")), "name", tr('ui.ea5b1406ca9d'))),
            ("Remote／Branch", _value(project, "github_remote") + " / " + _value(project, "github_branch")),
            (tr('ui.89ca87699127'), _value(project, "github_access", tr('ui.3a0d6a021156'))),
        ])

    # -- project workspaces (the drawer) ----------------------------------------------------------------
    def _work_rows(self, preset_id: str) -> list[WorkRow]:
        open_tabs = {tab.info.session_id: tab for tab in self.workspace.tabs() if tab.info.session_id and tab.alive()} \
            if hasattr(self, "workspace") else {}
        rows = []
        shown = {s["id"]: s.get("status", "unknown") for s in self._effective_sessions()}
        for session in env_state.project_sessions(preset_id, self._records.get("sessions", [])):
            account = self._by_id("accounts", _value(session, "account_id"))
            environment = self._by_id("environments", _value(session, "environment_id"))
            shell = _value(session, "mode") == "shell"
            title = tr('ui.9f7cfab95388') if shell else (TOOL_NAMES.get(account.get("tool"), tr('ui.fd201455c92e')) + (f" · {_value(account, 'name')}" if account else ""))
            attach, stop, remove, _ = self.session_actions(session)
            tab = open_tabs.get(session["id"])
            rows.append(WorkRow(session_id=session["id"], title=title,
                                sub=tr('ui.7f3221545a9b', v0=f"{_value(environment, 'name', tr('ui.13c355a95d42'))}", v1=f"{_display_time(_value(session, 'updated_at'))}"),
                                badge=env_state.session_badge(shown.get(session["id"], _value(session, "status", "unknown"))),
                                tab_id=tab.info.id if tab else "", can_attach=attach, can_stop=stop, can_remove=remove,
                                can_restart=env_state.can_restart(session, self._records.get("environments", []), self._records.get("accounts", []),
                                                      _value(self._by_id("presets", preset_id), "environment_id")),
                                workspace=self.workspace.workspace_name(tab.info.workspace_id) if tab else ""))
        if hasattr(self, "workspace"):              # open ones by desktop (picker order), newest first inside each
            desktops = {workspace_id: index for index, workspace_id in enumerate(self.workspace.desktop_ids())}
            place = {row.session_id: desktops.get(open_tabs[row.session_id].info.workspace_id, len(desktops))
                     for row in rows if row.tab_id}
            rows.sort(key=lambda row: place.get(row.session_id, len(desktops) + 1))
        return rows

    def _open_session_ids(self) -> set[str]:
        return {tab.info.session_id for tab in self.workspace.tabs() if tab.info.session_id and tab.alive()} if hasattr(self, "workspace") else set()

    def _restore_steps(self, preset_id: str) -> list[env_state.RestoreStep]:
        return env_state.restore_plan(preset_id, self._records.get("sessions", []), self._records.get("environments", []),
                                      self._records.get("accounts", []), self._open_session_ids(),
                                      _value(self._by_id("presets", preset_id), "environment_id"))

    def _describe_step(self, step: env_state.RestoreStep) -> str:
        session = step.session
        account = self._by_id("accounts", _value(session, "account_id"))
        what = tr('ui.9f7cfab95388') if _value(session, "mode") == "shell" else (TOOL_NAMES.get(account.get("tool"), tr('ui.fd201455c92e')) + f"（{_value(account, 'name')}）")
        place = _value(self._by_id("environments", _value(session, "environment_id")), "name", tr('ui.13c355a95d42'))
        return (tr('ui.a00149512642') if step.action == "attach" else tr('ui.25e74b781d2e')) + f"{what} · {place}"

    def restore_project(self, preset_id: str) -> None:
        """One go: re-attach what still runs on a server and start the stopped workspaces again, as they were."""
        project = self._by_id("presets", preset_id)
        if not project:
            return
        if hasattr(self, "workspace") and self._restore_in_workspace(preset_id):
            return
        steps = self._restore_steps(preset_id)
        if not steps:
            has_any = any(s.get("preset_id") == preset_id for s in self._records.get("sessions", []))
            self._notice(tr('ui.ecb5287a6979') if has_any else tr('ui.574742c2cc06'))
            return
        if len(steps) > 1 or any(step.action == "relaunch" for step in steps):
            lines = "\n".join("・" + self._describe_step(step) for step in steps)
            if not self._confirm(tr('ui.936eb92431ac'), tr('ui.7aabf9d080ac', v0=f"{_value(project, 'name')}", v1=f'{len(steps)}', v2=f'{lines}'),
                                 confirm_text=tr('ui.b927a75b85c9')):
                return
        done = 0
        for step in steps:
            done += bool(self._attach_step(step) if step.action == "attach" else self._relaunch(step.session))
        if done < len(steps):
            self._toast(tr('ui.8b4eb8291994', v0=f'{done}', v1=f'{len(steps)}'), "warn")

    def _restore_in_workspace(self, preset_id: str) -> bool:
        """Restoring a project means returning to the workspace it was in, as it was: show its open terminal, or bring
        that desktop's saved terminals back (re-attach tmux, continue the conversations). False: in no workspace."""
        page = self.workspace
        members = self._workspace_membership().get(preset_id, [])
        for workspace_id in members:
            tab = next((t for t in page.desktop_tabs(workspace_id) if t.info.preset_id == preset_id and t.alive()), None)
            if tab is not None:
                self.show_page("workspace")
                page.select(tab.info.id)
                return True
        for workspace_id in members:
            if self._pending_slots.get(workspace_id):
                self.restore_pending(workspace_id)
                return True
            ended = next((t for t in page.desktop_tabs(workspace_id) if t.info.preset_id == preset_id), None)
            if ended is not None:                       # still on that desktop, finished: start it again in its place
                if not self._confirm(tr("Restart program"), tr("This starts the program again and continues its conversation. It does not reconnect to a running process."),
                                     confirm_text=tr("Restart")):
                    return True
                if workspace_id != page.workspace_id:
                    self.open_workspace(workspace_id)
                if page.workspace_id == workspace_id:
                    self.show_page("workspace")
                    self.reload_terminal(ended.info.id)
                return True
            if workspace_id and not page.has_workspace(workspace_id):
                self.open_workspace(workspace_id)       # its saved terminals come back with it
                return True
        return False

    def _attach_step(self, step: env_state.RestoreStep) -> bool:
        return self._invoke("attach", session_id=step.session["id"])

    def _relaunch(self, session: dict[str, Any]) -> bool:
        """Start a stopped workspace again with the same service, account and environment; its old record is replaced."""
        preset_id = _value(session, "preset_id")
        project = self._by_id("presets", preset_id)
        if not project:
            self._notice(tr('ui.2da828daa28e'))
            return False
        if _value(session, "environment_id") != _value(project, "environment_id"):
            self._notice(tr('ui.d8fe9baacea9'))
            return False
        kwargs: dict[str, Any] = {"preset_id": preset_id, "environment_id": _value(session, "environment_id")}
        if _value(session, "mode") == "shell":
            kwargs["mode"] = "shell"
            if session.get("terminal_profile"):
                kwargs["terminal_profile"] = session["terminal_profile"]
        else:
            account = self._by_id("accounts", _value(session, "account_id"))
            if not account or not runtime_module.account_fits(account, kwargs["environment_id"] or "local"):
                self._notice(tr('ui.cecc661dd7e6'))
                return False
            kwargs["account_id"] = account["id"]
            kwargs["resume"] = True           # bring the conversation back, not just the program
            if runtime_module.switch_supported(account):
                options = self._quota_launch_options(session["id"], kwargs["environment_id"])
                kwargs.update(options)        # its exact conversation, last account and saved flow
        if not self._invoke("launch", **kwargs):
            return False
        self._carry_quota_policy(session["id"], getattr(self, "_last_launch_id", None))
        try:
            self.store.delete("sessions", session["id"])      # the new record takes its place
        except KeyError:
            pass
        self._records["sessions"] = self.store.list("sessions")
        return True

    def _work_restart(self, session_id: str) -> None:
        session = self._by_id("sessions", session_id)
        if session:
            self._relaunch(session)

    def _refresh_project_drawer(self) -> None:
        """Terminals opened or closed in the workspace: the drawer's "已在工作區開啟" marks follow."""
        if hasattr(self, "work_drawer") and self._selected_project and not self.work_drawer.isHidden():
            self.work_drawer.set_works(self._work_rows(self._selected_project), len(self._restore_steps(self._selected_project)))

    def _work_switch(self, tab_id: str) -> None:
        self.show_page("workspace")
        self.workspace.select(tab_id)

    def _work_attach(self, session_id: str) -> None:
        self._invoke("attach", session_id=session_id)

    def _work_stop(self, session_id: str) -> None:
        session = self._by_id("sessions", session_id)
        if session:
            self._stop_one(session)

    def _work_remove(self, session_id: str) -> None:
        session = self._by_id("sessions", session_id)
        if session:
            self._remove_one(session)

    # -- accounts / sessions --------------------------------------------------------------------------
    def _render_records(self, kind: str) -> None:
        if kind == "accounts":
            self.accounts_page.set_records(self._records.get("accounts", []), self._records.get("github_accounts", []),
                                           self._records.get("environments", []), self._records.get("auth_states", []))
            self._refresh_account_details()
            return
        listing = getattr(self, f"{kind}_list", None)
        if not listing:
            return
        records = self._records.get(kind, [])
        records.sort(key=lambda r: _value(r, "name").casefold())
        rows = []
        for record in records:
            extra = " · ".join(filter(None, (_value(record, "tool"), _value(record, "auth"), _value(record, "kind"), _value(record, "ssh_target"), _value(record, "model"))))
            if kind == "accounts":
                extra += " · " + self._binding_text(record)
            if kind == "accounts":
                for state in self._records.get("auth_states", []):
                    if state["id"].startswith(record["id"] + ":"):
                        environment = self._by_id("environments", state["id"].split(":", 1)[1])
                        extra += f"\n{_value(environment, 'name')} · {state['status']} · {_display_time(state['checked_at'])}"
            rows.append((_value(record, "name", tr('ui.67bee6acfc3c')) + (f"\n{extra}" if extra else ""), record.get("id"), ""))
        self._fill_list(listing, rows)
        if hasattr(self, f"{kind}_panel"):
            getattr(self, f"{kind}_panel").show_empty(not rows)

    def _render_environments(self) -> None:
        root = self.store.path.parent
        states = [env_state.build_env_state(env, records=self._records, root=root, pinned=env.get("id") in self.prefs.pinned)
                  for env in self._records.get("environments", [])]
        self.env_page.set_states(states, {env_id: set(kinds) for env_id, kinds in self._pending.items() if kinds})

    # -- home ---------------------------------------------------------------------------------
    def _build_home(self) -> QWidget:
        from .home_page import HomePage
        page = HomePage()
        page.page_requested.connect(self.show_page)
        page.project_selected.connect(self._show_project)
        page.open_requested.connect(self.open_terminal)
        page.restore_requested.connect(self.restore_project)
        page.add_project_requested.connect(self.add_project)
        page.updates_requested.connect(lambda: self.open_settings("about"))
        page.workspace_requested.connect(self.open_workspace)
        page.new_workspace_requested.connect(self.new_workspace)
        page.edit_workspace_requested.connect(self.edit_workspace)
        self.home_page = page
        return page

    def _render_home(self) -> None:
        if not hasattr(self, "home_page"):
            return
        root = self.store.path.parent
        states = [env_state.build_env_state(env, records=self._records, root=root, pinned=env.get("id") in self.prefs.pinned)
                  for env in self._records.get("environments", [])]
        self.home_page.set_data({**self._records, "sessions": self._effective_sessions()}, states, self.update_report(),
                                restorable=self._restorable_projects())
        if hasattr(self, "workspace"):
            self.home_page.set_workspaces(self._workspace_cards())

    def _remember_toast_anchor(self, anchor) -> None:
        if hasattr(self.prefs, "toast_anchor"):
            self.prefs.toast_anchor = list(anchor) if anchor else None
            self.prefs.save()

    def _show_project(self, preset_id: str) -> None:
        self._selected_project = preset_id
        self._project_env_filter = None
        self.show_page("projects")
        self._render_projects()

    # -- updates --------------------------------------------------------------------------------
    def app_version(self) -> str:
        from . import updates
        return updates.current_version()

    def update_report(self) -> dict[str, Any] | None:
        if self._update_report is None:
            from . import updates
            cached = updates.load_cached(self.store.path.parent) or {}
            # A result recorded by another version (before an update or downgrade) says nothing about this one.
            self._update_report = cached if cached.get("current") == updates.current_version() else {}
        return self._update_report or None

    def check_updates(self, force: bool = True) -> None:
        """Look for newer versions in the background (git tags + CHANGELOG); results arrive on ``_update_done``."""
        from . import updates
        if self._update_busy or (not force and not (self.UPDATE_AUTOCHECK and getattr(self.prefs, "update_check", True))):
            return
        cached = self.update_report() or {}
        checked = env_state.parse_time(str(cached.get("checked_at", "")))
        if not force and checked is not None and (datetime.now(timezone.utc) - checked).total_seconds() < 6 * 3600:
            return
        self._update_busy = True
        self.update_report_changed.emit()
        root = self.store.path.parent

        def work() -> None:
            try:
                report = updates.check(updates.repo_root(), root)
            except Exception as exc:     # never let a failed check reach the UI thread as an exception
                report = {"status": "unavailable", "message": str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__}
            self._update_done.emit(report if isinstance(report, dict) else {})

        threading.Thread(target=work, daemon=True).start()

    def _on_update_checked(self, report: dict) -> None:
        previous = (self._update_report or {}).get("latest")
        self._update_busy = False
        self._update_report = report
        self.update_report_changed.emit()
        self._render_home()
        if report.get("status") == "update_available" and report.get("latest") != previous:
            self._toast(tr("Version {latest} is available. Open Settings › About and updates to see what's new.",
                           latest=report.get("latest", "")), "info")

    def apply_update(self) -> None:
        """Back up settings, then show one update operation at a time."""
        active = getattr(self, "_update_dialog", None)
        if active is not None and active.in_progress():
            active.show()
            active.raise_()
            active.activateWindow()
            return
        from . import updates
        repo = updates.repo_root()
        report = self.update_report() or {}
        packaged = selfcmd.frozen()
        if repo is None and not packaged:
            self._notice(tr("This copy is not a Git checkout, so it cannot update itself. Download the new version instead."))
            return
        if not packaged:
            clean, reason = updates.working_tree_clean(repo)
            if not clean:
                self._notice(tr("The program folder has local changes, so updating is paused to keep them safe.") + "\n" + reason)
                return
        message = (tr("Your data is backed up first. The launcher will close and start again by itself; local terminals that are still running end (SSH work kept in tmux keeps running).")
                   if packaged else
                   tr("Your data is backed up first. Restart the launcher afterwards."))
        if not self._confirm(tr("Update Vibe Launcher"), message, confirm_text=tr("Update")):
            return
        from .settings_dialog import SettingsDialog
        for widget in QApplication.topLevelWidgets():          # Settings is modal and would cover the update window
            if isinstance(widget, SettingsDialog) and widget.isVisible():
                widget.reject()
        data = self.store.path.parent
        args = (selfcmd.command("updates", "apply-release", "--data", str(data)) if packaged
                else selfcmd.command("updates", "apply", "--repo", str(repo), "--data", str(data)))
        dialog = UpdateDialog(args, data, report.get("releases", []), self)
        dialog.launched.connect(self._installer_started)
        self._update_dialog = dialog
        dialog.show()
        dialog.start()

    def _installer_started(self) -> None:
        # apply_update already confirmed that updating ends local terminals.
        self._quitting = True
        self._installing_update = True

    def _by_id(self, kind: str, record_id: str) -> dict[str, Any]:
        return next((r for r in self._records.get(kind, []) if r.get("id") == record_id), {})

    def _binding_text(self, account: dict[str, Any]) -> str:
        if account.get("auth") == "api":
            return tr('ui.e8fd0720bd65')
        bound = _value(account, "environment_id")
        return tr('ui.c692396eef65') + _value(self._by_id("environments", bound), "name", bound) + "」" if bound else tr('ui.8d5ef742aefc')

    def _choice_list(self, kind: str, current_id: str | None = None, include_default: bool = False) -> list[tuple[str, str]]:
        rows = [(f"{r.get('name', tr('ui.67bee6acfc3c'))}" + (f" ({r.get('tool')})" if kind == "accounts" else ""), r.get("id", "")) for r in self._records.get(kind, [])]
        if include_default:
            rows.insert(0, (tr('ui.70d9626ae2fb'), ""))
        return rows

    # ------------------------------------------------------------------ projects: actions
    def add_project(self, seed: dict[str, Any] | None = None) -> bool:
        if not self._records.get("environments"):
            if self._confirm(tr('ui.61c3b29cc3d1'), tr('ui.142234341796'), confirm_text=tr('ui.f94551313ca6')):
                self.show_page("environments")
                self.add_record("environments")
            return False
        return self._edit_preset(seed=seed)

    def edit_project(self) -> None:
        record = self._by_id("presets", self._selected_project or "")
        if record:
            self._edit_preset(record)

    def _edit_preset(self, record: dict[str, Any] | None = None, seed: dict[str, Any] | None = None) -> bool:
        fields = [("name", tr('ui.c4f17fe66069'), tr('ui.3a0b3d8535dc')), ("environment_id", tr('ui.1a5673fa2641'), ""),
                  ("path", tr('ui.8cd44ab870e4'), r"C:\Projects\my-app"), ("tags", tr('ui.9b423a3fa339'), tr('ui.cef893a96312')),
                  ("account_id", tr('ui.198fcb03c1b7'), "")]
        accounts = [(tr('ui.2f5437bd882b'), "")] + [
            (f"{_value(a, 'name', tr('ui.67bee6acfc3c'))}（{TOOL_NAMES.get(a.get('tool'), a.get('tool'))}）· " + self._binding_text(a), a["id"])
            for a in sorted(self._records.get("accounts", []), key=lambda a: (_value(a, "tool"), _value(a, "name").casefold()))]
        choices = {"account_id": accounts, "environment_id": self._choice_list("environments")}
        values_in = {"persistent": True, "ask_account": True, **(record or seed or {})}
        values_in["tags"] = ", ".join(project_tags(values_in))
        extras = [("ask_account", tr('ui.3dd850951bce')),
                  ("persistent", tr('ui.947b40b3afa0'))]
        def check(values: dict[str, Any]) -> str:
            if not values.get("name") or not str(values.get("path", "")).strip():
                return tr('ui.35d7362bd0f7')
            try:
                normalize_tags(values.get("tags", ""))
            except ValueError as error:
                return str(error)
            chosen = self._by_id("accounts", values.get("account_id", ""))
            if chosen and not runtime_module.account_fits(chosen, values.get("environment_id", "")):
                place = _value(self._by_id("environments", _value(chosen, "environment_id")), "name", tr('ui.c8118f8f54c7'))
                return tr('ui.e8757e13eb46', v0=f"{_value(chosen, 'name')}", v1=f"{place}")
            return ""

        dialog = RecordDialog(tr('ui.6a51e329699a') if record else tr('ui.4efd9b7b6756'), fields, values_in, choices, self, extras, draft=True,
                              validate=check)
        kinds = {e["id"]: e.get("kind", "local") for e in self._records.get("environments", [])}
        known: dict[str, list[str]] = {}
        for preset in self._records.get("presets", []):
            known.setdefault(_value(preset, "environment_id"), [])
            if _value(preset, "path") not in known[_value(preset, "environment_id")]:
                known[_value(preset, "environment_id")].append(_value(preset, "path"))
        assist = PathAssist(dialog, "path", "environment_id", kinds, known, self._browse_remote)
        TagAssist(dialog, "tags", list(collect_tags(self._records.get("presets", []))))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        values = dialog.values()
        values["path"] = values["path"].strip()
        if not values["name"] or not values["path"]:
            self._notice(tr('ui.35d7362bd0f7'))
            return False
        try:
            values["tags"] = normalize_tags(values.get("tags", ""))
        except ValueError as error:
            self._notice(str(error))
            return False
        chosen = self._by_id("accounts", values.get("account_id", ""))
        if chosen and not runtime_module.account_fits(chosen, values["environment_id"]):
            place = _value(self._by_id("environments", _value(chosen, "environment_id")), "name", tr('ui.c8118f8f54c7'))
            self._notice(tr('ui.e8757e13eb46', v0=f"{_value(chosen, 'name')}", v1=f'{place}'))
            return False
        if assist.is_local() and not self._ensure_local_folder(values["path"]):
            return False
        values["id"] = (record or {}).get("id") or self._new_id()
        values["favorite"] = bool((record or {}).get("favorite", False))
        values["last_used"] = (record or {}).get("last_used", "")
        if self._save("presets", values):
            dialog.discard_draft()
        self._selected_project = values["id"]
        self._render_projects()
        return True

    def _browse_remote(self, environment_id: str, start: str) -> str:
        """Folder picker for an SSH environment (the PC cannot browse its disk): "" if the user cancels."""
        environment = self._by_id("environments", environment_id)
        if environment.get("kind") != "ssh":
            return ""
        root, database = self.store.path.parent, self.store.path

        def lister(path: str) -> dict[str, Any]:
            return remote_browse.list_remote(environment, path, root, database)
        dialog = RemoteFolderDialog(_value(environment, "name", environment_id), start, lister, self)
        return dialog.chosen() if dialog.exec() == QDialog.DialogCode.Accepted else ""

    def _confirm_create_folders(self, target: str, existing: Path | None, missing: list[Path]) -> bool:
        return CreateFoldersDialog(target, existing, missing, "", self).exec() == QDialog.DialogCode.Accepted

    def _ensure_local_folder(self, text: str) -> bool:
        """Local project path: if it does not exist, say so, show which folders would be created, and ask. False = stay in the dialog."""
        path = Path(text).expanduser()
        if not path.is_absolute() or path.is_dir():
            return True
        try:
            existing, missing = runtime_module.folder_plan(path)
        except ValueError as error:
            self._notice(str(error))
            return False
        if not self._confirm_create_folders(str(path), existing, missing):
            self._toast(tr('ui.f5f7273f3bd5'), "warn")
            return True
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self._notice(tr('ui.a240abd1b0fb', v0=f'{error.strerror or type(error).__name__}'))
            return False
        self._toast(tr('ui.90ed828f43c6', v0=f'{len(missing)}'))
        return True

    def _icon_clicked_in_list(self, item) -> None:
        self.project_list.setCurrentItem(item)          # also makes it the selected project
        later(self, 0, self.change_project_icon)

    def change_project_icon(self) -> None:
        """Pick a glyph, picture or SVG (and a colour) for the selected project. Nothing changes on Cancel."""
        from .project_icon_dialog import ProjectIconDialog
        project = self._by_id("presets", self._selected_project or "")
        if not project:
            return
        old = self.prefs.project_icons.get(project["id"])
        dialog = ProjectIconDialog(self, _value(project, "name"), old)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        root = self.store.path.parent
        try:
            entry = dialog.result_entry(root)
        except (OSError, ValueError) as exc:
            self._error(tr("Project icon"), exc)
            return
        self._apply_icon_choice(self.prefs.project_icons, project["id"], old, entry)
        self.prefs.save()
        self._refresh_project_icons()

    def _refresh_project_icons(self) -> None:
        project_icons.configure(self.store.path.parent, self.prefs.project_icons)
        self._render_projects()
        self._render_home()
        if self._selected_project:
            self.project_tile.set_project(self._selected_project)

    def _prune_project_icons(self) -> None:
        """Forget icons (and their copied pictures) of projects that no longer exist."""
        known = {p.get("id") for p in self._records.get("presets", [])}
        gone = [pid for pid in self.prefs.project_icons if pid not in known]
        for pid in gone:
            entry = self.prefs.project_icons.pop(pid)
            if entry["kind"] == "file":
                project_icons.remove_picture(self.store.path.parent, entry["name"])
        if gone:
            self.prefs.save()
            project_icons.configure(self.store.path.parent, self.prefs.project_icons)

    def _prune_workspace_icons(self) -> None:
        """Forget icons (and their copied pictures) of workspaces that were deleted."""
        known = {w.get("id") for w in self._records.get("workspaces", [])} | {DEFAULT_ICON_KEY}
        gone = [key for key in self.prefs.workspace_icons if key not in known]
        for key in gone:
            entry = self.prefs.workspace_icons.pop(key)
            if entry["kind"] == "file":
                project_icons.remove_picture(self.store.path.parent, entry["name"])
        if gone:
            self.prefs.save()

    def delete_project(self) -> None:
        if not self._selected_project:
            return
        project = self._by_id("presets", self._selected_project)
        if not self._confirm(tr('ui.16327a65bc18'), tr('ui.b7b1ca1969ae', v0=f"{_value(project, 'name')}"), confirm_text=tr('ui.3c8f5b363ab3'), danger=True):
            return
        self._delete("presets", self._selected_project)
        self._selected_project = None

    def _selected_record(self, kind: str) -> dict[str, Any]:
        if kind in {"accounts", "github_accounts"}:
            selected_kind, record_id = self.accounts_page.current_selection
            return self._by_id(kind, record_id) if selected_kind == kind else {}
        listing = getattr(self, f"{kind}_list")
        item = listing.currentItem()
        return self._by_id(kind, item.data(Qt.ItemDataRole.UserRole)) if item else {}

    def add_record(self, kind: str) -> None:
        self._edit_record(kind)

    def _add_account_tool(self, tool: str) -> None:
        if tool == "github":
            self._edit_github_account()
        elif tool in {"codex", "claude", "deepseek-harness"}:
            self._edit_record("accounts", {"tool": tool, "auth": "subscription"})

    def _edit_github_account(self, record: dict | None = None) -> None:
        fields = [("name", tr('ui.bda532c613dd'), tr('ui.784d37ea3afd')), ("author_name", tr('ui.1434b8c92478'), tr('ui.cf4613863b1e')),
                  ("author_email", "Commit email", tr('ui.34378823af5b'))]
        choices = {}
        if not record:
            fields.append(("environment_id", tr('ui.1106da49ddb5'), ""))
            choices["environment_id"] = self._choice_list("environments")
        dialog = RecordDialog(tr('ui.bc3a0bab5dfe'), fields, record, choices, self, draft=True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        if not values.get("name"):
            self._notice(tr('ui.276d155f1454'))
            return
        account = {**(record or {}), **values, "id": (record or {}).get("id") or self._new_id()}
        if not self._save("github_accounts", account):
            return
        dialog.discard_draft()
        self.accounts_page.select("github_accounts", account["id"])

    def _account_action(self, action: str, kind: str, record_id: str) -> None:
        record = self._by_id(kind, record_id)
        if not record or self.accounts_page.current_selection != (kind, record_id):
            return
        if kind == "github_accounts":
            if action == "edit":
                self._edit_github_account(record)
            elif action in {"login", "status"}:
                kwargs = {"github_account_id": record_id, "environment_id": record["environment_id"],
                          "operation": {"github_operation": action}}
                self._start_operation("github", kwargs, lambda result: self._github_result(None, kwargs, result))
            elif action == "delete":
                self.delete_record(kind)
        elif kind == "accounts":
            if action == "edit":
                self.edit_record(kind)
            elif action == "delete":
                self.delete_record(kind)
            elif action == "login":
                self.login_account()
            elif action in {"settings", "history"} and record.get("tool") != "deepseek-harness":
                self.import_profile(action)

    def _refresh_account_details(self) -> None:
        kind, record_id = self.accounts_page.current_selection
        record = self._by_id(kind, record_id) if kind else {}
        github = kind == "github_accounts"
        harness = record.get("tool") == "deepseek-harness"
        environment = self._by_id("environments", record.get("environment_id", ""))
        text = f"{record.get('name', '')} · {environment.get('name', tr('ui.2d1afa1675e1'))}"
        if github:
            text += tr('ui.03a48a23ef12', v0=f"{record.get('login') or tr('ui.43240f4f30b8')}", v1=f"{record.get('status', 'unknown')}")
            if record.get("author_email"):
                text += tr('ui.a88db175e428', v0=f"{record.get('author_name', '')}", v1=f"{record['author_email']}")
            text += tr('ui.3044e47c1ee8')
        elif harness:
            text += tr('ui.546db50f461d')
        self.account_details.setText(text)
        for action, button in self.account_buttons.items():
            button.setVisible(bool(record) and (action != "status" or github) and
                              (action not in {"settings", "history"} or (not github and not harness)))
        self.account_buttons["login"].setText(tr('ui.d8d75e2280d1') if harness else tr('ui.aad60e6f37b3'))
        self.quota_panel.setVisible(bool(record) and not github and not harness)
        self._refresh_quota_panel()

    def edit_record(self, kind: str) -> None:
        record = self._selected_record(kind)
        if record:
            self._edit_record(kind, record)

    def _edit_record(self, kind: str, record: dict[str, Any] | None = None) -> None:
        if kind == "accounts":
            fields = [("name", tr('ui.bda532c613dd'), tr('ui.37c353808d1d')), ("tool", tr('ui.5ca6730d4415'), ""), ("auth", tr('ui.ef0dddcd1137'), ""),
                      ("environment_id", tr('ui.960ef36b8360'), ""),
                      ("model", tr('ui.71793c015340'), tr('ui.75135e32674e')), ("base_url", tr('ui.4414da9b7b88'), tr('ui.9f9bfa9db495'))]
            envs = self._choice_list("environments")
            if record is not None and not record.get("environment_id") and record.get("auth") == "subscription":
                envs = [(tr('ui.6e26269590dd'), "")] + envs
            choices = {"tool": [("Codex", "codex"), ("Claude", "claude"), (tr('ui.7d9b62db4c19'), "deepseek-harness")],
                       "auth": [(tr('ui.1e83a84eb1a7'), "subscription"), ("API", "api")],
                       "environment_id": envs}
            title = tr('ui.921f75f57fee')
            checkboxes = [("allow_insecure_loopback", tr('ui.5cd971f0e2c1')),
                          ("share_project_history", tr("Share each project's conversations with my other accounts of the same tool"))]
        else:
            from .askpass import POLICIES
            fields = [("name", tr('ui.15914d68ecc2'), tr('ui.3ffa857ea258')), ("kind", tr('ui.1588dd8c9e73'), ""), ("ssh_target", tr('ui.caf05f1dd8ba'), "user@host"),
                      ("ssh_auth_mode", tr('ui.b35dd82ab5c4'), ""), ("remember_seconds", tr('ui.538d73fb75b6'), "")]
            choices = {"kind": [(tr('ui.019e29ea514a'), "local"), ("SSH", "ssh")], "remember_seconds": POLICIES,
                       "ssh_auth_mode": [(tr('ui.3547df85d8dc'), "interactive"), (tr('ui.0d3341abb4cc'), "password"), ("SSH key", "key")]}
            title = tr('ui.1a5673fa2641')
            checkboxes = []
        def check(values: dict[str, Any]) -> str:
            """Refusals shown inside the dialog, so what was typed stays put."""
            if not values.get("name"):
                return tr('ui.276d155f1454')
            if kind == "environments" and values.get("kind") == "ssh" and not values.get("ssh_target"):
                return tr('ui.d8a597ce9ba4')
            if kind == "accounts" and values.get("tool") == "deepseek-harness" and (
                    values.get("auth") != "subscription"
                    or self._by_id("environments", values.get("environment_id", "")).get("kind") != "ssh"):
                return tr('ui.af869b01737d')
            if (kind == "environments" and values.get("kind") == "ssh" and values.get("remember_seconds", 0)
                    and values.get("ssh_auth_mode") != "password"):
                return tr('ui.b12b64169d28')
            return ""

        dialog = RecordDialog(title, fields, record, choices, self, checkboxes, draft=True, validate=check)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        if not values["name"]:
            self._notice(tr('ui.276d155f1454'))
            return
        if kind == "environments" and values["kind"] == "ssh" and not values["ssh_target"]:
            self._notice(tr('ui.d8a597ce9ba4'))
            return
        values["id"] = (record or {}).get("id") or self._new_id()
        if kind == "accounts" and values.get("tool") == "deepseek-harness":
            if values.get("auth") != "subscription" or self._by_id("environments", values.get("environment_id", "")).get("kind") != "ssh":
                self._notice(tr('ui.af869b01737d'))
                return
            values.update(model="", base_url="", share_project_history=False)
        if kind == "accounts" and values.get("auth") == "api":
            values["environment_id"] = ""        # a key / endpoint is not tied to a machine
        if kind == "environments" and values["kind"] == "local":
            values["ssh_target"] = ""
            values.pop("remember_seconds", None)
            values.pop("ssh_auth_mode", None)
        if kind == "environments" and values["kind"] == "ssh" and values.get("remember_seconds", 0) and values.get("ssh_auth_mode") != "password":
            self._notice(tr('ui.b12b64169d28'))
            return
        if kind == "environments" and values.get("remember_seconds") == 0 and record:
            try:
                from .askpass import revoke_password
                revoke_password(record["id"], self.store.path.parent)
            except OSError as exc:
                self._error(tr('ui.0c8ff25c22d7'), exc)
                return
        if not self._save(kind, values):
            return
        dialog.discard_draft()
        if kind == "environments":
            self.env_page.select(values["id"])

    # ------------------------------------------------------------------ environments: actions
    def use_environment(self) -> None:
        """Primary action: open this environment's projects, or start a new project pre-filled with it."""
        environment = self._selected_record("environments")
        if not environment:
            self._notice(tr('ui.145a976c7728'))
            return
        env_id = environment["id"]
        if any(p.get("environment_id") == env_id for p in self._records.get("presets", [])):
            self._set_project_env_filter(env_id)
            self.show_page("projects")
            self._toast(tr('ui.f37db38efe97', v0=f"{_value(environment, 'name')}"))
            return
        if self.add_project(seed={"environment_id": env_id}):
            self._set_project_env_filter(env_id)
            self.show_page("projects")

    def toggle_pin(self, env_id: str) -> None:
        name = _value(self._by_id("environments", env_id), "name")
        if env_id in self.prefs.pinned:
            self.prefs.pinned.discard(env_id)
            self._toast(tr('ui.674f131239b2', v0=f'{name}'))
        else:
            self.prefs.pinned.add(env_id)
            self._toast(tr('ui.e1f98220bf85', v0=f'{name}'))
        self.prefs.save()
        self._render_environments()

    def forget_ssh_password(self) -> None:
        environment = self._selected_record("environments")
        if not environment or environment.get("kind") != "ssh":
            self._notice(tr('ui.a5d88693f203'))
            return
        try:
            from .askpass import revoke_password
            revoke_password(environment["id"], self.store.path.parent)
            self._toast(tr('ui.c341a00659da'))
        except OSError as exc:
            self._error(tr('ui.0c8ff25c22d7'), exc)

    def doctor_environment(self) -> None:
        environment = self._selected_record("environments")
        if not environment:
            self._notice(tr('ui.145a976c7728'))
            return
        self._invoke("doctor", environment_id=environment["id"])

    def check_tool_versions(self) -> None:
        environment = self._selected_record("environments")
        if not environment:
            self._notice(tr('ui.145a976c7728'))
            return
        self._invoke("check-tools", environment_id=environment["id"])

    def update_tool(self, tool: str) -> None:
        environment = self._selected_record("environments")
        if not environment:
            self._notice(tr('ui.145a976c7728'))
            return
        dialog = RecordDialog(tr('ui.259d72c036ab', v0=f'{tool}', v1=f"{environment['name']}"),
                              [("snap_channel", tr('ui.bda00c109bab'), "")],
                              {"snap_channel": ""},
                              {"snap_channel": [(tr('ui.ea3b4ba0225d'), ""), ("Snap latest/stable", "latest/stable"),
                                                (tr('ui.3fbb891cd946'), "latest/edge")]}, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._invoke("update-tool", environment_id=environment["id"], tool=tool, snap_channel=dialog.values()["snap_channel"])

    def inspect_ssh_settings(self) -> None:
        environment = self._selected_record("environments")
        if not environment or environment.get("kind") != "ssh":
            self._notice(tr('ui.a5d88693f203'))
            return
        self._invoke("ssh-settings", environment_id=environment["id"])

    def lock_ssh_password(self) -> None:
        environment = self._selected_record("environments")
        if not environment or environment.get("kind") != "ssh":
            self._notice(tr('ui.a5d88693f203'))
            return
        try:
            from .askpass import revoke_password
            revoke_password(environment["id"], self.store.path.parent)
            self._toast(tr('ui.3d010f011e02'))
        except OSError as exc:
            self._error(tr('ui.2fb2b0543a5d'), exc)

    def delete_record(self, kind: str) -> None:
        record = self._selected_record(kind)
        if not record:
            return
        if kind == "environments" and record.get("id") == "local":
            self._notice(tr('ui.0bd9bdc7d37d'))
            return
        extra = (tr('ui.39dfcb3f7b46') if kind == "environments" and record.get("kind") == "ssh" else
                 tr('ui.230264abd5fc') if kind in {"accounts", "github_accounts"} else tr('ui.9aeebb241ac5'))
        title = tr('ui.ea146c4bd274') if kind == "environments" else tr('ui.6b0041cf4c37')
        if not self._confirm(title, tr('ui.acfc206d11a0', v0=f"{_value(record, 'name')}", v1=f'{extra}'), confirm_text=tr('ui.3c8f5b363ab3'), danger=True):
            return
        if kind == "environments" and record.get("kind") == "ssh":
            try:
                from .askpass import revoke_password
                revoke_password(record["id"], self.store.path.parent)
            except Exception as exc:
                self._error(tr('ui.ead98675b967'), exc)
                return
        if kind == "environments":
            self.prefs.pinned.discard(_value(record, "id"))
            self.prefs.save()
            self._pending.pop(_value(record, "id"), None)
            if self._project_env_filter == record.get("id"):
                self._project_env_filter = None
        self._delete(kind, _value(record, "id"))

    def login_account(self) -> None:
        account = self._selected_record("accounts")
        if not account:
            self._notice(tr('ui.754d57d2a378'))
            return
        if account.get("tool") == "deepseek-harness":
            self.harness_settings(account=account)
            return
        environments = self._records.get("environments", [])
        if not environments:
            self._notice(tr('ui.6914a7a5440e'))
            return
        api_account = _value(account, "auth") == "api"
        method_choices = ([((tr('ui.a6a7ab2f4c5f')), "api")] if api_account else
                          [(tr('ui.3bc8e98643cd'), "device"), (tr('ui.8d821e5afe8f'), "browser")])
        bound = "" if api_account else _value(account, "environment_id")
        fields = ([] if bound else [("environment_id", tr('ui.1106da49ddb5'), "")]) + [("login_method", tr('ui.6a5b0c5cb695'), "")]
        choices = {"environment_id": self._choice_list("environments"), "login_method": method_choices}
        default_method = "api" if api_account else "device"
        title = tr('ui.f1681b591d0d') + (f"（{_value(self._by_id('environments', bound), 'name', bound)}）" if bound else "")
        dialog = RecordDialog(title, fields, {"login_method": default_method}, choices, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        environment = self._by_id("environments", bound or dialog.values()["environment_id"])
        method = dialog.values()["login_method"]
        if _value(account, "tool") == "claude":
            method = "api" if api_account else "browser"
        if method == "browser" and environment.get("kind") == "ssh":
            self._toast(tr('ui.013a059f305f'), "info")
        elif method == "api":
            self._toast(tr('ui.00ac624974e6'), "info")
        self._invoke("login", account_id=_value(account, "id"), environment_id=_value(environment, "id"), login_method=method)

    def launch_project(self) -> None:
        if not self._selected_project:
            return
        self.open_terminal(self._selected_project)

    def attach_project(self) -> None:
        if self._selected_project:
            self.restore_project(self._selected_project)

    def doctor_project(self) -> None:
        if not self._selected_project:
            return
        self._invoke("doctor", preset_id=self._selected_project)

    def toggle_favorite(self) -> None:
        if not self._selected_project:
            return
        project = self._by_id("presets", self._selected_project)
        if project:
            self._save("presets", {**project, "favorite": not bool(project.get("favorite"))})

    def refresh_auth(self) -> None:
        environments = self._records.get("environments", [])
        if not environments:
            return
        choices = {"environment_id": self._choice_list("environments")}
        dialog = RecordDialog(tr('ui.167c2536cd42'), [("environment_id", tr('ui.fba18681418f'), "")], {}, choices, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._invoke("refresh", environment_id=dialog.values()["environment_id"])

    def export_config(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, tr('ui.9263e6bbcdf7'), "vibe-launcher-config.json", tr('ui.67b92b689d9b'))
        if path:
            self._run_store(lambda: self.store.export_config(Path(path)), tr('ui.dcdfc56cee03'))

    def import_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr('ui.f5475897e4a5'), "", tr('ui.67b92b689d9b'))
        if path and self._confirm(tr('ui.f5475897e4a5'), tr('ui.6f571cb4c731'), confirm_text=tr('ui.20809c5d8596')):
            self._run_store(lambda: self.store.import_config(Path(path)), tr('ui.7c25609c4701'))

    def _diagnose(self, operation: str, error: Exception) -> None:
        # Never log terminal output, request contents, paths, or credential details.
        import hashlib
        logger = logging.getLogger("vibe_launcher.diagnostics." + hashlib.sha256(str(self.store.path.parent).encode()).hexdigest()[:16])
        if not logger.handlers:
            directory = self.store.path.parent / "diagnostics"
            runtime_module.private_dir(directory)
            path = directory / "launcher.log"
            runtime_module.assert_private_path(path)
            handler = RotatingFileHandler(path, maxBytes=65536, backupCount=2, encoding="utf-8")
            runtime_module.protect_path(path)
            logger.addHandler(handler)
            logger.setLevel(logging.WARNING)
            logger.propagate = False
        for index in (1, 2):
            runtime_module.assert_private_path(self.store.path.parent / "diagnostics" / ("launcher.log." + str(index)))
        logger.warning("%s: %s", operation, type(error).__name__)

    def detach_terminal(self, tab_id: str) -> None:
        tab = self.workspace.tab(tab_id)
        if tab is None:
            return
        self._start_operation("detach", {"session_id": tab.info.session_id,
                              "operation": {"attachment_nonce": tab.info.attachment_nonce}},
                              lambda result: self.workspace.confirm_detach(tab_id, result), quiet=True)

    def _start_operation(self, action: str, kwargs: dict, callback, *, quiet: bool = False) -> None:
        try:
            operation_id = self.service.start(action, **kwargs, spawn=self.workspace.spawn if self._embedded_enabled() else None)
            if not operation_id:
                raise RuntimeError(tr('ui.ff37f570a10a'))
            self._operations[operation_id] = {"callback": callback, "since": time.monotonic(), "action": action,
                                              "login": action == "login" or (action == "github" and kwargs.get("operation", {}).get("github_operation") == "login"),
                                              "embedded": self._embedded_enabled()}
            if self._operations[operation_id]["login"] and self._operations[operation_id]["embedded"]:
                self.show_page("workspace")
            if not quiet:
                self._toast(tr('ui.aa0e08683b0b'))
        except Exception as exc:
            self._error(tr('ui.6b46dba7af51'), exc)
            callback({"status": "failed", "error": str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__})

    def _poll_operations(self) -> None:
        for operation_id, info in list(self._operations.items()):
            path = self.store.path.parent / "results" / (operation_id + ".json")
            if path.exists():
                payload = json.loads(runtime_module.read_private(path, limit=65536))
                if payload.get("operation_id") != operation_id or payload.get("action") != info["action"] or not isinstance(payload.get("result"), dict):
                    raise ValueError(tr('ui.f59cf8780ca4'))
                if info.get("login") and "runner_exit_code" not in payload:
                    if time.monotonic() - info["since"] > 900:
                        del self._operations[operation_id]
                        info["callback"]({"status": "unknown", "message": tr('ui.e7c7bc4d8ba6')})
                    continue
                del self._operations[operation_id]
                path.unlink()
                if info.get("login"):
                    self._login_result(operation_id, payload["result"] if payload["runner_exit_code"] == 0 else {"status": "failed"}, info.get("embedded", False))
                info["callback"](payload["result"])
            elif time.monotonic() - info["since"] > (10 if info["action"] == "detach" else 900):
                del self._operations[operation_id]
                info["callback"]({"status": "unknown", "message": tr('ui.ea2fe04e5cf9')})

    def _login_result(self, operation_id: str, result: dict, embedded: bool) -> None:
        if embedded:
            self.workspace.confirm_login(operation_id, result.get("status", "unknown"))
        elif result.get("status") in {"authenticated", "configured"}:
            self._toast(tr('ui.40857ec8a467') if result["status"] == "configured" else tr('ui.72233aaf51c6'))

    def refresh_project_history(self, preset_id: str, account_id: str, environment_id: str) -> None:
        """Refresh this project's conversations from the native user home and other launcher profiles."""
        def done(result: dict) -> None:
            summary = result.get("summary") or {}
            if result.get("status") != "imported":
                self._toast(tr("Could not refresh conversations"), "warn")
            elif summary.get("copied"):
                self._toast(tr("Added {v0} conversation(s); use /resume in the terminal to pick them up", v0=str(summary["copied"])))
            else:
                self._toast(tr("No new conversations in this environment"))
        self._start_operation("import-history", {"preset_id": preset_id, "account_id": account_id, "environment_id": environment_id,
                                                  "operation": {"project_history": True}}, done)

    def import_profile(self, kind: str) -> None:
        account = self._selected_record("accounts")
        if not account or account.get("tool") == "deepseek-harness":
            self._notice(tr('ui.69953e0a96dd'))
            return
        environment_id = account.get("environment_id") or self.quota_panel.selected_environment() or "local"
        if not self._confirm(tr('ui.8568e2c12cf9'), tr('ui.1b2612239d33') + (tr('ui.96c628f7a15e') if kind == "settings" else tr('ui.9d39a49aef61')) + tr('ui.d40767623119'), confirm_text=tr('ui.20809c5d8596')):
            return
        self._start_operation("import-" + kind, {"account_id": account["id"], "environment_id": environment_id},
                              lambda result: self._notice(tr('ui.b6ec22e28635') + json.dumps(result, ensure_ascii=False)))

    # ------------------------------------------------------------------ service plumbing
    # -- account quota ---------------------------------------------------------------------------
    def _invoke(self, action: str, **kwargs: Any) -> bool:
        if action == "launch":
            self._last_launch_id = None             # set again only by a launch that really starts
        if action == "attach":
            tab = next((tab for tab in self.workspace.tabs() if tab.alive() and not tab.closing
                        and tab.info.session_id == kwargs.get("session_id")), None)
            if tab:
                self._work_switch(tab.info.id)
                return self.workspace.selected_id() == tab.info.id
        if action == "launch" and hasattr(self, "workspace") and "workspace_id" not in kwargs:
            kwargs["workspace_id"] = self._workspace_key()
        embedded = action in self.EMBEDDED_ACTIONS and self._embedded_enabled()
        try:
            if embedded:
                try:
                    operation_id = self.service.start(action, spawn=self.workspace.spawn, **kwargs)
                except Exception as exc:
                    if isinstance(exc, (ValueError, KeyError)):
                        raise
                    self._toast(tr('ui.c04f1d6dacd6'), "warn")
                    embedded = False
                    operation_id = self.service.start(action, **kwargs)
            else:
                operation_id = self.service.start(action, **kwargs)
        except Exception as exc:
            self._error(tr('ui.eb806c6ff81e'), exc)
            return False
        if action == "launch":
            self._last_launch_id = operation_id    # the new session, for state that follows a reopened terminal
        if embedded:
            self.show_page("workspace")
        if action == "login" and operation_id:
            self._operations[operation_id] = {"callback": lambda result: self.reload(), "since": time.monotonic(),
                                              "action": action, "login": True, "embedded": embedded}
        self._toast(tr('ui.eddda0df7bb2') if embedded else tr('ui.6d60b3cc8898'))
        self._mark_pending(action, kwargs)
        self._render_environments()  # show the checking state immediately, not on the next poll
        later(self, 250, self.reload)
        return True

    def _workspace_key(self) -> str:
        """The shown workspace's id; on a server its terminals share one tmux session named after it."""
        if self.workspace.workspace_id:
            return self.workspace.workspace_id
        if not self.prefs.default_workspace_id_saved:
            self.prefs.save()           # the default desktop keeps this id, so its tmux session is found again
        return self.prefs.default_workspace_id

    def _mark_pending(self, action: str, kwargs: dict[str, Any]) -> None:
        """Remember an environment-level check that finishes in an external terminal, to show a checking state."""
        env_id = kwargs.get("environment_id")
        if not env_id:
            return
        kind = {"doctor": "doctor", "ssh-settings": "ssh", "check-tools": "tools"}.get(action)
        if action == "update-tool":
            kind = f"update:{kwargs.get('tool')}"
        if not kind:
            return
        environment = self._by_id("environments", env_id)
        root = self.store.path.parent
        path = (env_state.tool_report_path(root, env_id) if kind in {"tools"} or kind.startswith("update:")
                else env_state.diagnostics_path(root, env_id) if environment.get("kind") == "ssh" else None)
        try:
            baseline = path.stat().st_mtime if path is not None and path.exists() else 0.0
        except OSError:
            baseline = 0.0
        ttl = 10 if path is None else 180
        self._pending.setdefault(env_id, {})[kind] = {"since": time.monotonic(), "path": path, "baseline": baseline, "ttl": ttl}

    def _poll_pending(self) -> None:
        for env_id in list(self._pending):
            for kind, info in list(self._pending[env_id].items()):
                path = info["path"]
                finished = False
                try:
                    finished = path is not None and path.exists() and path.stat().st_mtime > info["baseline"]
                except OSError:
                    finished = False
                if finished:
                    del self._pending[env_id][kind]
                    self._announce_result(env_id, kind)
                elif time.monotonic() - info["since"] > info["ttl"]:
                    del self._pending[env_id][kind]
                    if path is not None:
                        name = _value(self._by_id("environments", env_id), "name", tr('ui.fba18681418f'))
                        self._notice(tr('ui.30b76e3fd7bb', v0=f'{name}', v1=f"{info['ttl'] // 60}"))
            if not self._pending[env_id]:
                del self._pending[env_id]

    def _announce_result(self, env_id: str, kind: str) -> None:
        name = _value(self._by_id("environments", env_id), "name", tr('ui.fba18681418f'))
        if kind in {"doctor", "ssh"}:
            self._toast(tr('ui.4355d26c2648', v0=f'{name}') if kind == "doctor" else tr('ui.4f3d500fad3e', v0=f'{name}'))
            return
        reports = {r.tool: r for r in env_state.load_tool_reports(self.store.path.parent, env_id)}
        if kind == "tools":
            behind = [env_state.TOOL_LABEL[t] for t, r in reports.items() if r.status == "update_available"]
            self._toast(tr('ui.db9dc7858120', v0=f"{'、'.join(behind)}") if behind else tr('ui.1e4a52778397'), "warn" if behind else "ok")
            return
        tool = kind.split(":", 1)[1]
        report = reports.get(tool)
        if report is not None and report.status == "update_failed":
            self._toast(tr('ui.181d25940d51', v0=f'{env_state.TOOL_LABEL.get(tool, tool)}'), "danger")
        else:
            self._toast(tr('ui.905d0515a130', v0=f'{env_state.TOOL_LABEL.get(tool, tool)}'))

    def _save(self, kind: str, record: dict[str, Any]) -> bool:
        return self._run_store(lambda: self.store.save(kind, record), tr('ui.5fb39ba1078e'))

    def _delete(self, kind: str, record_id: str) -> bool:
        return self._run_store(lambda: self.store.delete(kind, record_id), tr('ui.5f59d03ef51d'))

    def _run_store(self, operation: Any, success: str) -> bool:
        try:
            operation()
            self.reload()
            self._toast(success)
            return True
        except Exception as exc:
            self._error(tr('ui.12022293daa0'), exc)
            return False

    def _new_id(self) -> str:
        from uuid import uuid4
        return str(uuid4())


_scale_from_prefs = False      # main() set QT_SCALE_FACTOR from the saved interface scale (not the user)


def applied_ui_scale() -> int:
    """The interface scale this process runs at, in percent."""
    try:
        return max(1, round(float(os.environ.get("QT_SCALE_FACTOR", "1")) * 100))
    except ValueError:
        return 100


def _load_prefs(store: Any):
    from .prefs import UiPrefs
    return UiPrefs(Path(store.path).parent / "ui-state.json")


def MainWindow(store: Any, service: Any, terminal_factory: Any = None) -> VibeLauncherWindow:
    """Compatibility constructor: ``MainWindow(store, service)``."""
    return VibeLauncherWindow(store, service, terminal_factory)


def main() -> int:
    """Create the app and resolve storage/service dependencies only at startup."""
    from .service import LauncherService, data_root
    from .store import Store

    global _scale_from_prefs
    from .prefs import stored_ui_scale
    # Interface scale (Settings → General) is a whole-window zoom; Qt only takes it before the application exists.
    # A QT_SCALE_FACTOR the user set themselves wins.
    scale = stored_ui_scale(data_root() / "ui-state.json")
    if scale != 100 and "QT_SCALE_FACTOR" not in os.environ:
        os.environ["QT_SCALE_FACTOR"] = str(scale / 100)
        _scale_from_prefs = True
    # Required before the application exists so the built-in terminal (Qt WebEngine) can share GL contexts.
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Vibe Launcher")
    store = Store(data_root() / "vibe-launcher.sqlite3")
    service = LauncherService(store)
    window = MainWindow(store, service)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
