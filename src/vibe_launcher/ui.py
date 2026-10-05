"""PySide6 desktop interface for the Vibe Launcher.

Layout: collapsible Sidebar + main workspace. All behaviour (store/service calls,
SSH/credential/CLI actions) lives on ``VibeLauncherWindow``, with the sessions page
(``ui_sessions``) and GitHub/Harness/quota (``ui_integrations``) mixed in; the visual components
come from ``kit`` / ``env_page`` and all styling from ``theme`` tokens.
"""

from __future__ import annotations

import html
import json
import logging
from logging.handlers import RotatingFileHandler
import sys
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
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
    QVBoxLayout,
    QWidget,
)

from . import env_state, remote_browse, theme
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
    Command,
    CommandPalette,
    ConfirmDialog,
    EmptyState,
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
    later,
    make_button,
    retheme_widgets,
)
from .appearance import AppearanceDialog
from .theme import C, R, S, Size, T, TOOL_ICONS, section_hue

TOOL_NAMES = {"codex": "Codex", "claude": "Claude", "deepseek-harness": "DeepSeek Harness（實驗性）"}
from .work_drawer import WorkDrawer, WorkRow
from .workspace_page import WorkspacePage
from .ui_format import _display_time, _value
from .ui_integrations import IntegrationActions
from .ui_sessions import SessionActions


def _kv_html(pairs: list[tuple[str, str]]) -> str:
    """Muted key above a normal-weight value, escaped. Easier to scan than a flat text dump."""
    blocks = []
    for key, value in pairs:
        blocks.append(f'<div style="margin-bottom:10px"><span style="color:{C.text_muted};font-size:{T.meta}px">{html.escape(key)}</span><br>'
                      f'<span style="color:{C.text}">{html.escape(value)}</span></div>')
    return "".join(blocks)


class RecordDialog(QDialog):
    """Small metadata-only editor. Secrets are deliberately never requested."""

    def __init__(self, title: str, fields: list[tuple[str, str, str]], values: dict[str, Any] | None = None,
                 choices: dict[str, list[tuple[str, str]]] | None = None, parent: QWidget | None = None,
                 checkboxes: list[tuple[str, str]] | None = None, draft: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self.setStyleSheet(f"QLabel {{ color: {C.text_secondary}; }}")
        self.fields: dict[str, QWidget] = {}
        values = values or {}
        choices = choices or {}
        root = QVBoxLayout(self)
        root.setContentsMargins(S.xl, S.xl, S.xl, S.lg)
        root.setSpacing(S.lg)
        heading = QLabel(title)
        heading.setProperty("role", "section")
        heading.setStyleSheet(f"font-size: {T.dialog_title}px; color: {C.text};")
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
            widget.setChecked(bool(values.get(key, key == "share_extensions")))
            self.fields[key] = widget
            form.addRow(widget)
        root.addLayout(form)
        self.draft = None
        store = getattr(parent, "store", None)
        if draft and store is not None:
            # What was typed survives closing the dialog by accident or a save that is refused afterwards.
            self.draft = FormDraft(self, store.path.parent, f"record:{title}:{values.get('id') or 'new'}", self.fields)
            if self.draft.restored:
                note = QHBoxLayout()
                text = QLabel("已還原上次未儲存的輸入。")
                text.setProperty("role", "muted")
                note.addWidget(text, 1)
                note.addWidget(make_button("捨棄並還原原值", "ghost", small=True, callback=lambda: self._discard(values)))
                root.addLayout(note)
        buttons = QHBoxLayout()
        buttons.setSpacing(S.sm)
        buttons.addStretch(1)
        cancel = make_button("取消", "ghost")
        save = make_button("儲存", "primary")
        save.setDefault(True)
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        root.addLayout(buttons)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        theme.apply_dark_titlebar(self)

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
                widget.setChecked(bool(values.get(key, key == "share_extensions")))
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


def _surface(layout: QVBoxLayout | QHBoxLayout, margins: int = S.xl) -> QFrame:
    frame = QFrame()
    frame.setProperty("surface", "card")
    frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    layout.setContentsMargins(margins, margins, margins, margins)
    frame.setLayout(layout)
    return frame


class VibeLauncherWindow(SessionActions, IntegrationActions, QMainWindow):
    PAGES = ("projects", "workspace", "accounts", "environments", "sessions")
    EMBEDDED_ACTIONS = {"launch", "attach", "login", "quota"}
    _quota_done = Signal(str, str, str)       # account id, environment id, error text ("" on success)

    def __init__(self, store: Any, service: Any, terminal_factory: Any = None):
        super().__init__()
        self.store = store
        self.service = service
        self._terminal_factory = terminal_factory
        self.prefs = _load_prefs(store)
        theme.apply_theme(self.prefs.theme, self.prefs.accent)
        terminal_module.configure(font_size=self.prefs.terminal_font_size, cursor_blink=self.prefs.cursor_blink)
        self.setWindowTitle("Vibe Launcher")
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
        from .harness import TunnelManager
        self.tunnels = TunnelManager(store.path.parent)
        self._build()
        self.reload()
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        for account in self._records.get("accounts", []):
            if account.get("manual_import_notice"):
                self._toast("個人設定與對話已改為手動匯入；既有 profile 與對話均保留。", "warn")
                self.store.save("accounts", {**account, "manual_import_notice": False})

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        self.setStyleSheet(theme.build_stylesheet())
        self.setFont(theme.font())
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.sidebar = Sidebar([("projects", "專案", "folder"), ("workspace", "工作區", "square-terminal"),
                                ("accounts", "帳號", "user"), ("environments", "環境", "layers"),
                                ("sessions", "工作階段", "activity")])
        self.sidebar.set_collapsed(self.prefs.sidebar_collapsed, animate=False, emit=False)
        self.sidebar.navigate.connect(self.show_page)
        self.sidebar.import_requested.connect(self.import_config)
        self.sidebar.export_requested.connect(self.export_config)
        self.sidebar.appearance_requested.connect(self.open_appearance)
        self.sidebar.collapse_toggled.connect(self._remember_sidebar)
        self.nav_buttons = self.sidebar.buttons

        self.stack = QStackedWidget()
        self._pages["projects"] = self._build_projects()
        self._pages["workspace"] = self._build_workspace()
        self._pages["accounts"] = self._build_accounts()
        self._pages["environments"] = self._build_environments()
        self._pages["sessions"] = self._build_sessions()
        for key in self.PAGES:
            self.stack.addWidget(self._pages[key])
        outer.addWidget(self.sidebar)
        outer.addWidget(self.stack, 1)

        self.toasts = ToastManager(central)
        self.palette = CommandPalette(self)
        self._install_shortcuts()
        self.show_page("projects")
        self._apply_mode()
        later(self, 700, self._startup_housekeeping)

    FINISHED_KEPT = 20

    def _startup_housekeeping(self) -> None:
        """Tidy the session history and tell the user what the last run left behind (they would not otherwise know)."""
        try:
            self._records["sessions"] = self.store.list("sessions")
            self._reap_closed_terminals()
            finished = sorted((s for s in self._records["sessions"] if _value(s, "status") in {"ended", "failed"}),
                              key=lambda s: _value(s, "updated_at"), reverse=True)
            for session in finished[self.FINISHED_KEPT:]:
                self.store.delete("sessions", session["id"])
            self._records["sessions"] = self.store.list("sessions")
            self._render_sessions()
            counts = self._session_counts()
        except Exception:
            return
        if counts["unknown"] or counts["live"]:
            parts = ([f"{counts['live']} 個仍在進行"] if counts["live"] else []) + ([f"{counts['unknown']} 個狀態不明"] if counts["unknown"] else [])
            self._toast("上次留下 " + "、".join(parts) + "的工作階段。到「工作階段」頁可以檢查、停止或清除。")

    def _install_shortcuts(self) -> None:
        def bind(sequence: str, callback: Any) -> None:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(callback)

        bind("Ctrl+K", self.open_palette)
        bind("Ctrl+B", self.toggle_sidebar)
        bind("Ctrl+F", self.focus_search)
        bind("Ctrl+N", self.new_for_page)
        for index, key in enumerate(self.PAGES, start=1):
            bind(f"Ctrl+{index}", lambda k=key: self.show_page(k))

        # Lists: Enter does the main thing, Delete removes (each still asks to confirm).
        def bind_list(listing: QListWidget, enter: Any, delete: Any) -> None:
            for keys, callback in ((("Return", "Enter"), enter), (("Delete",), delete)):
                for sequence in keys:
                    shortcut = QShortcut(QKeySequence(sequence), listing)
                    shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
                    shortcut.activated.connect(callback)

        bind_list(self.project_list, lambda: self.launch_project(), lambda: self.delete_project())
        bind_list(self.sessions_list, lambda: self.attach_session(), lambda: self.remove_session())

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
        header = PageHeader("專案", "選擇專案，用官方 Codex 或 Claude CLI 在終端開啟", icon="folder", hue=section_hue("projects"))
        self.search = SearchField("搜尋專案名稱或路徑")
        self.search.setMinimumWidth(160)
        self.search.setMaximumWidth(300)
        self.search.textChanged.connect(self._render_projects)
        self.sort = QComboBox()
        self.sort.addItem("最近使用", "recent")
        self.sort.addItem("名稱", "name")
        self.sort.setAccessibleName("排序")
        self.sort.currentIndexChanged.connect(self._render_projects)
        self.favorite_filter = QCheckBox("只看收藏")
        self.favorite_filter.stateChanged.connect(self._render_projects)
        header.add_action(self.search, 1)
        header.add_action(self.sort)
        header.add_action(self.favorite_filter)
        header.add_action(make_button("新增專案", "primary", "plus", callback=lambda *_: self.add_project()))
        root.addWidget(header)

        self.tag_bar = TagFilterBar()
        self.tag_bar.set_selected(self.prefs.project_tags)
        self.tag_bar.changed.connect(self._project_tags_changed)
        root.addWidget(self.tag_bar)

        self.project_env_chip = make_button("", "subtle", "x", small=True, tip="清除環境篩選", callback=lambda *_: self._set_project_env_filter(None))
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
        empty = EmptyState()
        empty.set_content("folder", "還沒有專案", "新增專案設定資料夾、帳號與執行環境；啟動時會開啟官方 CLI 終端。", primary="新增專案")
        empty.primary.clicked.connect(lambda *_: self.clear_project_filters() if self.project_list_filtered else self.add_project())
        self.project_list_filtered = False
        self.project_panel = ListPanel(self.project_list, empty)

        details = QVBoxLayout()
        details.setSpacing(S.md)
        self.project_title = QLabel("選取一個專案")
        self.project_title.setProperty("role", "section")
        self.project_title.setStyleSheet(f"font-size: {T.dialog_title}px;")
        self.project_title.setWordWrap(True)
        self.project_info = QLabel("建立專案設定後，即可用官方 Codex 或 Claude CLI 開啟。")
        self.project_info.setProperty("role", "muted")
        self.project_info.setWordWrap(True)
        self.project_info.setTextFormat(Qt.TextFormat.RichText)
        self.project_info.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
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
        self.project_tile = IconTile(36, 18, radius=R.md)
        self.project_tile.set_icon("folder", section_hue("projects"))
        title_row.addWidget(self.project_tile, 0, Qt.AlignmentFlag.AlignTop)
        title_row.addWidget(self.project_title, 1)
        details.addLayout(title_row)
        details.addWidget(info_scroll, 1)
        actions_host = QWidget()
        actions = FlowLayout(actions_host, S.sm, S.sm)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.addWidget(make_button("啟動終端", "primary", "terminal", callback=lambda *_: self.launch_project()))
        actions.addWidget(make_button("SSH 終端", "secondary", "square-terminal", tip="只連線並進入專案資料夾，不執行 Codex 或 Claude",
                                      callback=lambda *_: self.open_shell()))
        actions.addWidget(make_button("接回工作", "secondary", "activity", tip="接回還在伺服器上執行的工作，並重新開啟已停止的工作區",
                                      callback=lambda *_: self.attach_project()))
        self.favorite_button = make_button("收藏", "ghost", "star", callback=lambda *_: self.toggle_favorite())
        self.favorite_button.setParent(actions_host)
        self.favorite_button.hide()
        project_more = IconButton("more", "更多動作", size=36, icon_size=18)
        project_more.clicked.connect(lambda: self._menu(project_more, [
            MenuAction("GitHub 設定…", self.github_settings, "folder"),
            MenuAction("開啟 GitHub repository", self.open_github_repository, "folder"),
            MenuAction("Harness Web…", self.harness_settings, "terminal"),
            MenuAction("診斷", self.doctor_project, "check-circle"),
            MenuAction("收藏／取消收藏", self.toggle_favorite, "star"),
            MenuAction("編輯專案", self.edit_project, "pencil"),
            MenuAction("刪除專案…", self.delete_project, "trash", danger=True)]))
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
        for action, title, icon in (("login", "登入…", "key"), ("edit", "編輯", "pencil"),
                                     ("status", "檢查登入", "refresh"), ("settings", "匯入設定／擴充", "folder"),
                                     ("history", "匯入既有對話", "folder"), ("delete", "移除紀錄…", "trash")):
            button = make_button(title, "primary" if action == "login" else "secondary", icon,
                                 callback=lambda *_, action=action: page.request_action(action))
            self.account_buttons[action] = button
            controls.addWidget(button)
        page.add_detail_widget(controls_host)
        return page

    def _build_sessions(self) -> QWidget:
        page = QWidget()
        root = self._page_root(page)
        header = PageHeader("工作階段", "每次「啟動終端」都會留下一筆工作階段：顯示它現在是運行中、已停止或失敗，並讓你接回或停止。",
                            icon="activity", hue=section_hue("sessions"))
        header.add_action(make_button("更新工作狀態", "secondary", "refresh", callback=lambda *_: self.refresh_auth()))
        root.addWidget(header)
        bar = QWidget()
        bar_row = QHBoxLayout(bar)
        bar_row.setContentsMargins(0, 0, 0, 0)
        bar_row.setSpacing(S.sm)
        self.session_summary = QLabel()
        self.session_summary.setProperty("role", "muted")
        self.session_summary.setWordWrap(True)
        bar_row.addWidget(self.session_summary, 1)
        self.session_check_button = make_button("檢查狀態不明的", "secondary", "refresh", small=True, callback=lambda *_: self.check_unknown_sessions(),
                                                tip="重新向伺服器確認這些工作是否還在跑；本機的則直接標為已結束")
        self.session_clean_button = make_button("清除已結束的", "ghost", "trash", small=True, callback=lambda *_: self.clean_finished_sessions(),
                                                tip="移除所有「已結束」與「失敗」的本機紀錄（不影響仍在執行的工作）")
        bar_row.addWidget(self.session_check_button)
        bar_row.addWidget(self.session_clean_button)
        root.addWidget(bar)
        self.sessions_list = QListWidget()
        self.sessions_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.sessions_list.setAccessibleDescription("可用 Ctrl 或 Shift 複選，一次停止或移除多筆")
        empty = EmptyState()
        empty.set_content("activity", "尚無工作階段", "從專案頁啟動後，工作階段會顯示在這裡。", primary="前往專案")
        empty.primary.clicked.connect(lambda *_: self.show_page("projects"))
        self.sessions_panel = ListPanel(self.sessions_list, empty)
        root.addWidget(self.sessions_panel, 1)
        controls_host = QWidget()
        controls = FlowLayout(controls_host, S.sm, S.sm)
        controls.setContentsMargins(0, 0, 0, 0)
        self.session_attach_button = make_button("接回終端", "primary", "terminal", callback=lambda *_: self.attach_session(),
                                                 tip="重新開啟 SSH 終端，接回仍在伺服器上運行的工作")
        self.session_stop_button = make_button("停止工作階段", "danger", "x-circle", callback=lambda *_: self.stop_session(),
                                               tip="可複選：同一個環境的會在一次連線中全部停止")
        self.session_remove_button = make_button("移除本機紀錄", "ghost", "trash", callback=lambda *_: self.remove_session(),
                                                 tip="只刪除這台電腦上的紀錄，不會停止遠端工作")
        controls.addWidget(self.session_attach_button)
        controls.addWidget(self.session_stop_button)
        controls.addWidget(self.session_remove_button)
        self.session_hint = QLabel()
        self.session_hint.setProperty("role", "muted")
        self.session_hint.setWordWrap(True)
        root.addWidget(self.session_hint)
        root.addWidget(controls_host)
        self.sessions_list.currentItemChanged.connect(lambda *_: self._sync_session_actions())
        self.sessions_list.itemSelectionChanged.connect(self._sync_session_actions)
        self.auth_summary = QLabel()
        self.auth_summary.setProperty("role", "meta")
        self.auth_summary.setWordWrap(True)
        root.addWidget(self.auth_summary)
        return page

    # -- workspace -------------------------------------------------------------------------------
    def _build_workspace(self) -> QWidget:
        page = WorkspacePage(terminal_factory=self._terminal_factory)
        page.composer_default = self.prefs.composer_default
        page.draft_root = self.store.path.parent
        self.workspace = page
        page.set_layout(self.prefs.workspace_layout)
        page.launch_requested.connect(self.open_terminal)
        page.palette_requested.connect(self.open_palette)
        page.notify.connect(lambda message, tone: self._toast(message, tone))
        page.save_workspace_requested.connect(self.save_workspace)
        page.open_workspace_requested.connect(self.open_workspace)
        page.delete_workspace_requested.connect(self.delete_workspace)
        page.layout_changed.connect(self._remember_layout)
        page.terminals_changed.connect(self._refresh_project_drawer)
        page.restore_requested.connect(self.restore_project)
        page.launcher.set_tag_filter(self.prefs.workspace_tags)
        page.launcher.tags_changed.connect(self._workspace_tags_changed)
        return page

    def _remember_layout(self, mode: str) -> None:
        self.prefs.workspace_layout = mode
        self.prefs.save()

    def _embedded_enabled(self) -> bool:
        """Use the built-in terminal when the service supports it, it is not switched off, and this machine can host it."""
        if not getattr(self.service, "supports_spawn", False) or self.prefs.external_terminal:
            return False
        return self._terminal_factory is not None or terminal_module.available()

    # ------------------------------------------------------------------ appearance
    def appearance_settings(self) -> dict[str, Any]:
        p = self.prefs
        return {"theme": p.theme, "accent": p.accent, "terminal_font_size": p.terminal_font_size,
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
        theme.apply_theme(p.theme, p.accent)
        self.setStyleSheet(theme.build_stylesheet())
        theme.apply_dark_titlebar(self)
        retheme_widgets()
        terminal_module.retheme_all()
        terminal_module.configure(font_size=p.terminal_font_size, cursor_blink=p.cursor_blink)
        self.workspace.composer_default = p.composer_default
        theme.theme_bus().changed.emit()
        if save:
            p.save()

    def open_appearance(self) -> None:
        kept = AppearanceDialog.ask(self, self.appearance_settings(), lambda s: self.apply_appearance(s, save=False))
        self.apply_appearance(kept if kept is not None else self.appearance_settings(), save=kept is not None)

    def set_theme(self, theme_id: str) -> None:
        self.apply_appearance({"theme": theme_id})
        self._toast(f"已套用主題「{theme.THEMES[theme_id].name}」。")

    def toggle_external_terminal(self) -> None:
        self.prefs.external_terminal = not self.prefs.external_terminal
        self.prefs.save()
        self._toast("之後的終端會在獨立的外部視窗開啟。" if self.prefs.external_terminal else "之後的終端會在「工作區」內開啟。")

    def open_terminal(self, preset_id: str, kind: str = "agent", account_id: str = "", environment_id: str = "") -> None:
        """Open a project in the workspace: its agent (Codex/Claude) or just an SSH shell, optionally on another account/host."""
        if not self._by_id("presets", preset_id):
            self._notice("找不到這個專案，可能已被刪除。")
            return
        preset = self._by_id("presets", preset_id)
        kwargs: dict[str, Any] = {"preset_id": preset_id}
        if kind == "shell":
            kwargs["mode"] = "shell"        # SSH only: no account involved
        else:
            if not account_id:
                account_id = self._pick_account(preset, environment_id or _value(preset, "environment_id", "local"))
                if not account_id:
                    return
            kwargs["account_id"] = account_id
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
        signed = "已登入" if state and state.get("status") == "authenticated" else "尚未確認登入（第一次會要求登入）"
        return f"訂閱 · {signed}"

    def _ask_account(self, preset: dict[str, Any], environment_name: str, options: list[dict[str, Any]], selected: str) -> tuple[str, bool] | None:
        dialog = AccountChoiceDialog(_value(preset, "name", "專案"), environment_name, options, selected, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.chosen()

    def _pick_account(self, preset: dict[str, Any], environment_id: str) -> str:
        """The account to open ``preset`` with on ``environment_id``: remembered one, the only one, or the user's choice."""
        eligible = self._eligible_accounts(environment_id)
        if not eligible:
            self._notice("這個環境還沒有可用的帳號。訂閱帳號的登入只存在它綁定的環境，請到「帳號」頁新增帳號並選擇此環境（或新增 API 帳號）。")
            return ""
        remembered = _value(preset, "account_id")
        if preset.get("ask_account", True) is False and any(a["id"] == remembered for a in eligible):
            return remembered
        if len(eligible) == 1:
            return eligible[0]["id"]
        environment = self._by_id("environments", environment_id)
        options = [{"id": a["id"], "name": _value(a, "name", "未命名"), "tool": a.get("tool", ""), "detail": self._account_detail(a, environment_id)}
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
        if not slots:
            self._notice("目前沒有可儲存的終端。先從專案開啟幾個終端，再儲存成工作區。")
            return
        dialog = RecordDialog("儲存工作區", [("name", "工作區名稱", "例如：工地系統（前後端＋伺服器）")], {"name": ""}, None, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.values()["name"]
        if not name:
            self._notice("請填寫工作區名稱。")
            return
        existing = next((w for w in self._records.get("workspaces", []) if w.get("name") == name), None)
        record = {"id": (existing or {}).get("id") or self._new_id().replace("-", ""), "name": name,
                  "layout": self.workspace.area.mode, "slots": slots}
        self._save("workspaces", record)
        self._toast(f"已{'更新' if existing else '儲存'}工作區「{name}」（{len(slots)} 個終端）。")

    def open_workspace(self, workspace_id: str) -> None:
        workspace = self._by_id("workspaces", workspace_id)
        if not workspace:
            return
        opened = skipped = 0
        for slot in workspace.get("slots", []):
            project = self._by_id("presets", slot.get("preset_id", ""))
            if project:
                account_id = slot.get("account_id", "")
                if slot.get("environment_id") and slot.get("environment_id") != project.get("environment_id"):
                    account_id = ""         # saved before projects were tied to one host: open on the project's own, and pick again
                self.open_terminal(slot["preset_id"], slot.get("kind", "agent"), account_id, "")
                opened += 1
            else:
                skipped += 1
        self.workspace.set_layout(workspace.get("layout", "tabs"))
        self.show_page("workspace")
        if skipped:
            self._toast(f"已開啟 {opened} 個終端；{skipped} 個因專案已刪除而略過。", "warn")

    def delete_workspace(self, workspace_id: str) -> None:
        workspace = self._by_id("workspaces", workspace_id)
        if workspace and self._confirm("刪除工作區", f"要刪除工作區「{_value(workspace, 'name')}」嗎？不會影響專案或正在執行的終端。",
                                       confirm_text="刪除", danger=True):
            self._delete("workspaces", workspace_id)

    def _render_workspace(self) -> None:
        if hasattr(self, "workspace"):
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

    def closeEvent(self, event) -> None:  # noqa: N802
        running = self.workspace.running_count() if hasattr(self, "workspace") else 0
        if running and not self._confirm(
                "關閉 Vibe Launcher",
                f"還有 {running} 個終端連線中。關閉後，使用 tmux 在伺服器背景執行的工作會繼續，之後可從「工作階段」接回；"
                "直接執行（沒有 tmux）的工作會結束。", confirm_text="關閉並離開", danger=True):
            event.ignore()
            return
        if hasattr(self, "workspace"):
            self.workspace.shutdown()
        self.tunnels.shutdown()
        terminal_module.release_warm_page()
        super().closeEvent(event)

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

    # -- command palette -----------------------------------------------------------------------
    def open_palette(self) -> None:
        self.palette.set_commands(self._commands())
        self.palette.open_palette()

    def _commands(self) -> list[Command]:
        env = self._selected_record("environments")
        env_name = _value(env, "name")
        has_env = bool(env)
        has_ssh = has_env and env.get("kind") == "ssh"
        on_env = f"對「{env_name}」執行" if has_env else "請先在環境頁選取環境"
        commands = [
            Command("env-new", "快速新增環境", "新增 Local 或 SSH 環境", "plus", "環境", lambda: (self.show_page("environments"), self.add_record("environments")), "add create ssh local"),
            Command("env-use", "使用此環境", on_env, "arrow-right", "環境", self.use_environment, "connect open project", has_env),
            Command("env-check", "檢查環境", on_env, "check-circle", "環境", self.doctor_environment, "doctor check", has_env),
            Command("env-ssh", "SSH 設定", on_env if has_ssh else "僅限 SSH 環境", "sliders", "環境", self.inspect_ssh_settings, "ssh config identity proxyjump", has_ssh),
            Command("tools-check", "檢查 CLI 更新", on_env, "refresh", "開發工具", self.check_tool_versions, "codex claude version update", has_env),
            Command("tools-codex", "更新 Codex", on_env, "arrow-up-circle", "開發工具", lambda: self.update_tool("codex"), "update upgrade", has_env),
            Command("tools-claude", "更新 Claude", on_env, "arrow-up-circle", "開發工具", lambda: self.update_tool("claude"), "update upgrade", has_env),
            Command("project-new", "新增專案", "設定資料夾、帳號與環境", "folder", "專案", self.add_project, "create"),
            Command("account-new", "新增帳號", "Codex 或 Claude", "user", "帳號", lambda: (self.show_page("accounts"), self.add_record("accounts")), "create"),
            Command("config-import", "匯入設定", "從 JSON 檔還原", "download", "設定", self.import_config),
            Command("config-export", "匯出設定", "儲存為 JSON 檔", "upload", "設定", self.export_config),
            Command("sidebar", "收合／展開側邊欄", "Ctrl+B", "panel-left", "檢視", self.toggle_sidebar),
            Command("appearance", "外觀與主題…", "配色、強調色、終端字級與輸入列", "palette", "外觀", self.open_appearance,
                    "theme color appearance dark light font 主題 配色 顏色"),
        ]
        for theme_id, palette in theme.THEMES.items():
            current = theme_id == self.prefs.theme
            commands.append(Command(f"theme-{theme_id}", f"主題：{palette.name}", "目前使用中" if current else
                                    ("淺色" if not palette.dark else "深色"), "palette", "外觀",
                                    lambda i=theme_id: self.set_theme(i), f"theme 主題 {theme_id}", not current))
        pages = (("projects", "專案", "folder"), ("workspace", "工作區", "square-terminal"), ("accounts", "帳號", "user"),
                 ("environments", "環境", "layers"), ("sessions", "工作階段", "activity"))
        for index, (key, label, icon) in enumerate(pages, start=1):
            commands.append(Command(f"go-{key}", f"前往 {label}", f"Ctrl+{index}", icon, "前往", lambda k=key: self.show_page(k)))
        for number, tab in enumerate(self.workspace.tabs(), start=1):
            commands.append(Command(f"term-{tab.info.id}", f"切換到 {tab.info.project or '終端'} · {tab.info.label}",
                                    f"{tab.info.environment}　Alt+{number}" if number < 10 else tab.info.environment,
                                    "square-terminal" if tab.info.tool == "shell" else TOOL_ICONS.get(tab.info.tool, "terminal"), "終端",
                                    lambda i=tab.info.id: (self.show_page("workspace"), self.workspace.select(i)), "switch terminal"))
        for project in sorted(self._records.get("presets", []), key=lambda p: _value(p, "name").casefold()):
            name = _value(project, "name")
            commands.append(Command(f"open-shell-{project['id']}", f"開啟 {name} · SSH 終端", "只連線並進入專案資料夾", "square-terminal", "開啟",
                                    lambda i=project["id"]: self.open_terminal(i, "shell"), "ssh shell terminal"))
            commands.append(Command(f"open-agent-{project['id']}", f"開啟 {name} · Codex／Claude", "使用專案設定的帳號", "terminal", "開啟",
                                    lambda i=project["id"]: self.open_terminal(i, "agent"), "codex claude agent"))
        commands.append(Command("layout-tabs", "工作區：分頁檢視", "一次顯示一個終端", "square-terminal", "檢視",
                                lambda: (self.show_page("workspace"), self.workspace.set_layout("tabs"))))
        commands.append(Command("layout-grid", "工作區：並排檢視", "同時顯示多個終端", "grid", "檢視",
                                lambda: (self.show_page("workspace"), self.workspace.set_layout("grid"))))
        commands.append(Command("terminal-mode", "使用內建終端" if self.prefs.external_terminal else "改用外部終端視窗",
                                "終端在工作區內開啟" if self.prefs.external_terminal else "終端在獨立的視窗開啟", "repeat", "檢視",
                                self.toggle_external_terminal, "external internal window"))
        for state in env_state.sort_states(list(self.env_page._states.values())):
            commands.append(Command(f"switch-{state.id}", f"切換到 {state.name}", state.subtitle, "server" if state.kind == "ssh" else "monitor", "切換環境",
                                    lambda i=state.id: self.goto_environment(i), f"{state.target} {state.kind} switch"))
        return commands

    def goto_environment(self, env_id: str) -> None:
        self.show_page("environments")
        self.env_page.select(env_id)
        self.env_page.environments_list.setFocus()

    # -- toasts / dialogs -------------------------------------------------------------------------
    def _toast(self, message: str, tone: str = "ok") -> None:
        self.toasts.show(message, tone)

    def _confirm(self, title: str, message: str, *, confirm_text: str = "確認", danger: bool = False) -> bool:
        return ConfirmDialog.ask(self, title, message, confirm_text=confirm_text, danger=danger)

    def _notice(self, message: str) -> None:
        self.toasts.show(message, "warn", 8000 if len(message) > 40 else None)

    def _error(self, title: str, exc: Exception) -> None:
        # Avoid displaying exception text that might include command arguments or secrets.
        message = str(exc) if isinstance(exc, ValueError) else f"操作失敗：{type(exc).__name__}。請檢查資料後重試。"
        self.toasts.show(f"{title}：{message}", "danger")

    # ------------------------------------------------------------------ data
    def reload(self) -> None:
        try:
            self._records = {kind: self.store.list(kind) for kind in ("accounts", "environments", "presets", "sessions", "auth_states", "workspaces", "github_accounts", "web_services")}
            self._render_projects()
            self._render_records("accounts")
            self._render_environments()
            self._render_sessions()
            self._render_workspace()
            self._refresh_quota_panel()
        except Exception as exc:
            self._error("讀取資料失敗", exc)

    def _poll(self) -> None:
        # Re-read SQLite state while preserving the currently selected project/session.
        selected = self._selected_project
        try:
            old_sessions = self._records.get("sessions", [])
            old_auth = self._records.get("auth_states", [])
            self._records["sessions"] = self.store.list("sessions")
            self._records["auth_states"] = self.store.list("auth_states")
            self._selected_project = selected
            self._reap_closed_terminals()
            if self._records["sessions"] != old_sessions:
                self._render_workspace()
                self._render_sessions()
                self._render_projects()
            if self._records["auth_states"] != old_auth:
                self._render_records("accounts")
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
            self.statusBar().showMessage("狀態更新失敗；保留上次資料，稍後自動重試。")
            if time.monotonic() - self._last_poll_error >= 30:
                self._last_poll_error = time.monotonic()
                self._diagnose("poll", exc)

    def _reap_closed_terminals(self) -> None:
        """A terminal window that was closed takes its runner with it, so the status it last wrote can never advance.
        Move such sessions to "unknown" (they may still run remotely): the user can then refresh to find out."""
        changed = False
        for session in self._records.get("sessions", []):
            pid = session.get("runner_pid")
            status = session.get("status")
            local = self._by_id("environments", _value(session, "environment_id")).get("kind") == "local"
            if local and status in {"connecting", "waiting_auth", "running", "detached", "unknown"}:
                # Nothing keeps a local session alive once its terminal is gone, so there is nothing "unknown" about it.
                if not pid or not runner_module.process_alive(pid):
                    self.store.save("sessions", {**session, "status": "ended", "updated_at": runtime_module.now()})
                    changed = True
                continue
            if pid and status in {"connecting", "waiting_auth"} and not runner_module.process_alive(pid):
                self.store.save("sessions", {**session, "status": "unknown", "updated_at": runtime_module.now()})
                changed = True
        if changed:
            self._records["sessions"] = self.store.list("sessions")

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
        if self.sort.currentData() == "name":
            projects.sort(key=lambda p: _value(p, "name").casefold())
        else:
            projects.sort(key=lambda p: _value(p, "last_used"), reverse=True)
        env = self._by_id("environments", self._project_env_filter or "")
        self.project_chip_row.setVisible(bool(self._project_env_filter))
        self.project_env_chip.setText(f"環境：{_value(env, 'name', '未知')}")
        rows = []
        for project in projects:
            favorite = bool(project.get("favorite"))
            tags = " ".join("#" + t for t in project_tags(project))
            label = ("★ " if favorite else "☆ ") + (_value(project, "name") or "未命名專案") + "\n" + (tags + "  ·  " if tags else "") + _value(project, "path")
            rows.append((label, project.get("id"), _value(project, "path"), env_state.project_badge(project.get("id"), self._records.get("sessions", [])),
                         ("folder", "accent")))
        self._fill_list(self.project_list, rows)
        if not self.project_list.currentItem() and self.project_list.count():
            self.project_list.setCurrentRow(0)
        has_any = bool(self._records.get("presets"))
        self.project_list_filtered = has_any and not self.project_list.count()
        if self.project_list_filtered:
            self.project_panel.empty.set_content("search", "沒有符合的專案", "目前的搜尋、收藏、環境或標籤篩選沒有符合的專案。",
                                                 primary="清除篩選")
        else:
            self.project_panel.empty.set_content("folder", "還沒有專案",
                                                 "新增專案設定資料夾、帳號與執行環境；啟動時會開啟官方 CLI 終端。", primary="新增專案")
        self.project_panel.show_empty(not self.project_list.count())
        if not self.project_list.count():
            self._selected_project = None
            self.work_drawer.hide()
            self.project_title.setText("還沒有專案" if not has_any else "沒有符合的專案")
            self.project_info.setText("按「新增專案」設定資料夾與執行環境。開啟時再選擇帳號與服務。" if not has_any
                                      else "調整搜尋、環境或標籤篩選即可看到其他專案。")
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
        self.tag_bar.set_selected([])
        self.prefs.project_tags = []
        self.prefs.save()
        self._render_projects()

    def _project_selected(self, current: QListWidgetItem | None, _: QListWidgetItem | None = None) -> None:
        if not current:
            return
        self._selected_project = current.data(Qt.ItemDataRole.UserRole)
        project = next((p for p in self._records.get("presets", []) if p.get("id") == self._selected_project), {})
        account = self._by_id("accounts", _value(project, "account_id"))
        if account and not runtime_module.account_fits(account, _value(project, "environment_id", "local")):
            account = {}
        environment = self._by_id("environments", _value(project, "environment_id"))
        self.project_title.setText(("★ " if project.get("favorite") else "☆ ") + _value(project, "name", "未命名專案"))
        self.favorite_button.setText("取消收藏" if project.get("favorite") else "收藏")
        self.work_drawer.show()
        self.work_drawer.set_works(self._work_rows(_value(project, "id")), len(self._restore_steps(_value(project, "id"))))
        status = env_state.project_status(_value(project, "id"), self._records.get("sessions", []))
        if environment.get("kind") == "local":
            run_mode = "在本機終端視窗執行；關閉視窗會結束工作"
        elif project.get("persistent", True) is False:
            run_mode = "直接執行（不用 tmux）：關閉終端就會結束"
        else:
            run_mode = "背景保持（tmux）：關閉終端後工作仍繼續，可再接回"
        self.project_info.setText(_kv_html([
            ("狀態", env_state.session_badge(status)[0] if status else "尚未啟動"),
            ("執行方式", run_mode),
            ("路徑", _value(project, "path", "未設定")),
            ("標籤", "、".join(project_tags(project)) or "無"),
            ("帳號", (f"{_value(account, 'name')} · {_value(account, 'auth')}（上次使用）" if account else "")
             + ("" if account and project.get("ask_account", True) is False else "開啟時選擇" if not account else "；開啟時可更換")),
            ("服務", f"{_value(account, 'tool').title()} · {account.get('model') or '使用 CLI 預設模型'}" if account else "Codex 或 Claude，開啟時選擇"),
            ("環境", _value(environment, "name", "未設定")),
            ("Repository", _value(project, "github_repo") or ("尚未設定 remote（可在 GitHub 精靈補上）"
                                                             if _value(project, "github_account_id") else "尚未綁定")),
            ("GitHub 帳號", _value(self._by_id("github_accounts", _value(project, "github_account_id")), "name", "尚未綁定")),
            ("Remote／Branch", _value(project, "github_remote") + " / " + _value(project, "github_branch")),
            ("存取檢查", _value(project, "github_access", "尚未檢查")),
            ("最近使用", _display_time(_value(project, "last_used"))),
        ]))

    # -- project workspaces (the drawer) ----------------------------------------------------------------
    def _work_rows(self, preset_id: str) -> list[WorkRow]:
        open_tabs = {tab.info.session_id: tab.info.id for tab in self.workspace.tabs() if tab.info.session_id and tab.alive()} \
            if hasattr(self, "workspace") else {}
        rows = []
        for session in env_state.project_sessions(preset_id, self._records.get("sessions", [])):
            account = self._by_id("accounts", _value(session, "account_id"))
            environment = self._by_id("environments", _value(session, "environment_id"))
            shell = _value(session, "mode") == "shell"
            title = "SSH 終端" if shell else (TOOL_NAMES.get(account.get("tool"), "終端") + (f" · {_value(account, 'name')}" if account else ""))
            attach, stop, remove, _ = self.session_actions(session)
            rows.append(WorkRow(session_id=session["id"], title=title,
                                sub=f"{_value(environment, 'name', '未知環境')} · 更新於 {_display_time(_value(session, 'updated_at'))}",
                                badge=env_state.session_badge(_value(session, "status", "unknown")),
                                tab_id=open_tabs.get(session["id"], ""), can_attach=attach, can_stop=stop, can_remove=remove,
                                can_restart=env_state.can_restart(session, self._records.get("environments", []), self._records.get("accounts", []),
                                                      _value(self._by_id("presets", preset_id), "environment_id"))))
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
        what = "SSH 終端" if _value(session, "mode") == "shell" else (TOOL_NAMES.get(account.get("tool"), "終端") + f"（{_value(account, 'name')}）")
        place = _value(self._by_id("environments", _value(session, "environment_id")), "name", "未知環境")
        return ("接回 " if step.action == "attach" else "重新開啟 ") + f"{what} · {place}"

    def restore_project(self, preset_id: str) -> None:
        """One go: re-attach what still runs on a server and start the stopped workspaces again, as they were."""
        project = self._by_id("presets", preset_id)
        if not project:
            return
        steps = self._restore_steps(preset_id)
        if not steps:
            has_any = any(s.get("preset_id") == preset_id for s in self._records.get("sessions", []))
            self._notice("這個專案的工作區都已經開著或正在連線中，沒有需要召回的。" if has_any else "這個專案還沒有工作區可以召回。先按「啟動終端」開啟一個。")
            return
        if len(steps) > 1:
            lines = "\n".join("・" + self._describe_step(step) for step in steps)
            if not self._confirm("召回工作區", f"要召回「{_value(project, 'name')}」的 {len(steps)} 個工作區嗎？\n{lines}\nSSH 的每個終端可能各自需要驗證。",
                                 confirm_text="全部召回"):
                return
        done = 0
        for step in steps:
            done += bool(self._attach_step(step) if step.action == "attach" else self._relaunch(step.session))
        if done < len(steps):
            self._toast(f"召回了 {done}/{len(steps)} 個工作區，其餘請看提示。", "warn")

    def _attach_step(self, step: env_state.RestoreStep) -> bool:
        return self._invoke("attach", session_id=step.session["id"])

    def _relaunch(self, session: dict[str, Any]) -> bool:
        """Start a stopped workspace again with the same service, account and environment; its old record is replaced."""
        preset_id = _value(session, "preset_id")
        project = self._by_id("presets", preset_id)
        if not project:
            self._notice("這個工作區所屬的專案已被刪除，無法重新開啟。")
            return False
        if _value(session, "environment_id") != _value(project, "environment_id"):
            self._notice("這個工作區是在專案原本綁定之外的環境開啟的；專案只會在它綁定的環境執行，請用「啟動終端」重新開啟。")
            return False
        kwargs: dict[str, Any] = {"preset_id": preset_id, "environment_id": _value(session, "environment_id")}
        if _value(session, "mode") == "shell":
            kwargs["mode"] = "shell"
        else:
            account = self._by_id("accounts", _value(session, "account_id"))
            if not account or not runtime_module.account_fits(account, kwargs["environment_id"] or "local"):
                self._notice("這個工作區的帳號已被刪除，或登入不在這個環境，無法重新開啟。請改用「啟動終端」選擇帳號。")
                return False
            kwargs["account_id"] = account["id"]
        if not self._invoke("launch", **kwargs):
            return False
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
            rows.append((_value(record, "name", "未命名") + (f"\n{extra}" if extra else ""), record.get("id"), ""))
        self._fill_list(listing, rows)
        if hasattr(self, f"{kind}_panel"):
            getattr(self, f"{kind}_panel").show_empty(not rows)

    def _render_environments(self) -> None:
        root = self.store.path.parent
        states = [env_state.build_env_state(env, records=self._records, root=root, pinned=env.get("id") in self.prefs.pinned)
                  for env in self._records.get("environments", [])]
        self.env_page.set_states(states, {env_id: set(kinds) for env_id, kinds in self._pending.items() if kinds})

    def _by_id(self, kind: str, record_id: str) -> dict[str, Any]:
        return next((r for r in self._records.get(kind, []) if r.get("id") == record_id), {})

    def _binding_text(self, account: dict[str, Any]) -> str:
        if account.get("auth") == "api":
            return "API・任何環境"
        bound = _value(account, "environment_id")
        return "登入在「" + _value(self._by_id("environments", bound), "name", bound) + "」" if bound else "未綁定環境"

    def _choice_list(self, kind: str, current_id: str | None = None, include_default: bool = False) -> list[tuple[str, str]]:
        rows = [(f"{r.get('name', '未命名')}" + (f" ({r.get('tool')})" if kind == "accounts" else ""), r.get("id", "")) for r in self._records.get(kind, [])]
        if include_default:
            rows.insert(0, ("不指定", ""))
        return rows

    # ------------------------------------------------------------------ projects: actions
    def add_project(self, seed: dict[str, Any] | None = None) -> bool:
        if not self._records.get("environments"):
            if self._confirm("先設定執行環境", "專案需要一個執行環境（本機或 SSH 主機）。要先新增環境嗎？", confirm_text="新增環境"):
                self.show_page("environments")
                self.add_record("environments")
            return False
        return self._edit_preset(seed=seed)

    def edit_project(self) -> None:
        record = self._by_id("presets", self._selected_project or "")
        if record:
            self._edit_preset(record)

    def _edit_preset(self, record: dict[str, Any] | None = None, seed: dict[str, Any] | None = None) -> bool:
        fields = [("name", "專案名稱", "例如：工地影像分析"), ("environment_id", "執行環境", ""),
                  ("path", "資料夾路徑", r"C:\Projects\my-app"), ("tags", "標籤", "用逗號分隔，例如：工地, 前端"),
                  ("account_id", "預設帳號", "")]
        accounts = [("每次開啟時選擇", "")] + [
            (f"{_value(a, 'name', '未命名')}（{TOOL_NAMES.get(a.get('tool'), a.get('tool'))}）· " + self._binding_text(a), a["id"])
            for a in sorted(self._records.get("accounts", []), key=lambda a: (_value(a, "tool"), _value(a, "name").casefold()))]
        choices = {"account_id": accounts, "environment_id": self._choice_list("environments")}
        values_in = {"persistent": True, "ask_account": True, **(record or seed or {})}
        values_in["tags"] = ", ".join(project_tags(values_in))
        extras = [("ask_account", "每次開啟專案時讓我選擇帳號與服務（Codex／Claude）"),
                  ("persistent", "關閉終端後讓工作繼續執行（SSH 專案使用 tmux，伺服器需安裝 tmux；關閉則直接執行，關終端就結束）")]
        dialog = RecordDialog("編輯專案" if record else "新增專案", fields, values_in, choices, self, extras, draft=True)
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
            self._notice("請填寫專案名稱與資料夾路徑。")
            return False
        try:
            values["tags"] = normalize_tags(values.get("tags", ""))
        except ValueError as error:
            self._notice(str(error))
            return False
        chosen = self._by_id("accounts", values.get("account_id", ""))
        if chosen and not runtime_module.account_fits(chosen, values["environment_id"]):
            place = _value(self._by_id("environments", _value(chosen, "environment_id")), "name", "其他環境")
            self._notice(f"帳號「{_value(chosen, 'name')}」的登入綁定在「{place}」，不能用在這個專案的環境。請改選帳號，或選「每次開啟時選擇」。")
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
            self._toast("沒有建立資料夾；啟動專案時還會再詢問一次。", "warn")
            return True
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self._notice(f"無法建立資料夾：{error.strerror or type(error).__name__}")
            return False
        self._toast(f"已建立 {len(missing)} 層資料夾。")
        return True

    def delete_project(self) -> None:
        if not self._selected_project:
            return
        project = self._by_id("presets", self._selected_project)
        if not self._confirm("刪除專案設定", f"要刪除「{_value(project, 'name')}」的啟動設定嗎？不會刪除資料夾本身。", confirm_text="刪除", danger=True):
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
        fields = [("name", "顯示名稱", "我的 GitHub"), ("author_name", "Commit 名稱", "登入後自動帶入"),
                  ("author_email", "Commit email", "登入後自動帶入 GitHub noreply email")]
        choices = {}
        if not record:
            fields.append(("environment_id", "登入環境", ""))
            choices["environment_id"] = self._choice_list("environments")
        dialog = RecordDialog("GitHub 帳號", fields, record, choices, self, draft=True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        if not values.get("name"):
            self._notice("請填寫名稱。")
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
        text = f"{record.get('name', '')} · {environment.get('name', '所有環境')}"
        if github:
            text += f"\nGitHub 登入帳號：{record.get('login') or '尚未取得'} · {record.get('status', 'unknown')}"
            if record.get("author_email"):
                text += f"\nCommit 身分：{record.get('author_name', '')} <{record['author_email']}>"
            text += "\n官方 gh 認證儲存方式尚未驗證；Launcher 不讀取憑證。"
        elif harness:
            text += "\nDeepSeek Harness（實驗性）；登入、模型及對話由官方 Web 管理。"
        self.account_details.setText(text)
        for action, button in self.account_buttons.items():
            button.setVisible(bool(record) and (action != "status" or github) and
                              (action not in {"settings", "history"} or (not github and not harness)))
        self.account_buttons["login"].setText("官方 Web 登入…" if harness else "登入…")
        self.quota_panel.setVisible(bool(record) and not github and not harness)
        self._refresh_quota_panel()

    def edit_record(self, kind: str) -> None:
        record = self._selected_record(kind)
        if record:
            self._edit_record(kind, record)

    def _edit_record(self, kind: str, record: dict[str, Any] | None = None) -> None:
        if kind == "accounts":
            fields = [("name", "顯示名稱", "我的 Codex"), ("tool", "工具", ""), ("auth", "驗證方式", ""),
                      ("environment_id", "登入環境（訂閱帳號專用）", ""),
                      ("model", "模型（API 必填）", "訂閱可使用 CLI 預設模型"), ("base_url", "API Base URL（選填）", "空白使用官方 API")]
            envs = self._choice_list("environments")
            if record is not None and not record.get("environment_id") and record.get("auth") == "subscription":
                envs = [("（尚未綁定）", "")] + envs
            choices = {"tool": [("Codex", "codex"), ("Claude", "claude"), ("DeepSeek Harness（SSH Web，實驗性）", "deepseek-harness")],
                       "auth": [("訂閱登入", "subscription"), ("API", "api")],
                       "environment_id": envs}
            title = "帳號資料"
            checkboxes = [("allow_insecure_loopback", "允許 HTTP loopback endpoint（僅本機測試）")]
        else:
            from .askpass import POLICIES
            fields = [("name", "環境名稱", "例如：測試機"), ("kind", "類型", ""), ("ssh_target", "SSH 目標", "user@host"),
                      ("ssh_auth_mode", "SSH 驗證", ""), ("remember_seconds", "記住 SSH 密碼", "")]
            choices = {"kind": [("本機", "local"), ("SSH", "ssh")], "remember_seconds": POLICIES,
                       "ssh_auth_mode": [("互動驗證（預設）", "interactive"), ("密碼（可選快取）", "password"), ("SSH key", "key")]}
            title = "執行環境"
            checkboxes = []
        dialog = RecordDialog(title, fields, record, choices, self, checkboxes, draft=True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        if not values["name"]:
            self._notice("請填寫名稱。")
            return
        if kind == "environments" and values["kind"] == "ssh" and not values["ssh_target"]:
            self._notice("SSH 環境需要填寫連線目標，例如 user@host。")
            return
        values["id"] = (record or {}).get("id") or self._new_id()
        if kind == "accounts" and values.get("tool") == "deepseek-harness":
            if values.get("auth") != "subscription" or self._by_id("environments", values.get("environment_id", "")).get("kind") != "ssh":
                self._notice("Harness 帳號請選訂閱登入與 SSH 環境；模型、登入和對話由官方 Web 管理。")
                return
            values.update(model="", base_url="", share_extensions=False, import_history=False)
        if kind == "accounts" and values.get("auth") == "api":
            values["environment_id"] = ""        # a key / endpoint is not tied to a machine
        if kind == "environments" and values["kind"] == "local":
            values["ssh_target"] = ""
            values.pop("remember_seconds", None)
            values.pop("ssh_auth_mode", None)
        if kind == "environments" and values["kind"] == "ssh" and values.get("remember_seconds", 0) and values.get("ssh_auth_mode") != "password":
            self._notice("要保存 SSH 密碼，請將驗證方式設為「密碼（可選快取）」。密碼＋OTP 請選互動驗證，避免將 OTP 誤當密碼保存。")
            return
        if kind == "environments" and values.get("remember_seconds") == 0 and record:
            try:
                from .askpass import revoke_password
                revoke_password(record["id"], self.store.path.parent)
            except OSError as exc:
                self._error("無法清除已存密碼", exc)
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
            self._notice("請先選取環境。")
            return
        env_id = environment["id"]
        if any(p.get("environment_id") == env_id for p in self._records.get("presets", [])):
            self._set_project_env_filter(env_id)
            self.show_page("projects")
            self._toast(f"顯示使用「{_value(environment, 'name')}」的專案")
            return
        if self.add_project(seed={"environment_id": env_id}):
            self._set_project_env_filter(env_id)
            self.show_page("projects")

    def toggle_pin(self, env_id: str) -> None:
        name = _value(self._by_id("environments", env_id), "name")
        if env_id in self.prefs.pinned:
            self.prefs.pinned.discard(env_id)
            self._toast(f"已取消釘選「{name}」")
        else:
            self.prefs.pinned.add(env_id)
            self._toast(f"已釘選「{name}」")
        self.prefs.save()
        self._render_environments()

    def forget_ssh_password(self) -> None:
        environment = self._selected_record("environments")
        if not environment or environment.get("kind") != "ssh":
            self._notice("請先選取 SSH 環境。")
            return
        try:
            from .askpass import revoke_password
            revoke_password(environment["id"], self.store.path.parent)
            self._toast("已忘記密碼；下次需要驗證時重新輸入。")
        except OSError as exc:
            self._error("無法清除已存密碼", exc)

    def doctor_environment(self) -> None:
        environment = self._selected_record("environments")
        if not environment:
            self._notice("請先選取環境。")
            return
        self._invoke("doctor", environment_id=environment["id"])

    def check_tool_versions(self) -> None:
        environment = self._selected_record("environments")
        if not environment:
            self._notice("請先選取環境。")
            return
        self._invoke("check-tools", environment_id=environment["id"])

    def update_tool(self, tool: str) -> None:
        environment = self._selected_record("environments")
        if not environment:
            self._notice("請先選取環境。")
            return
        dialog = RecordDialog(f"更新 {tool} · {environment['name']}",
                              [("snap_channel", "Snap 頻道（非 Snap 請保留預設）", "")],
                              {"snap_channel": ""},
                              {"snap_channel": [("沿用目前來源／頻道", ""), ("Snap latest/stable", "latest/stable"),
                                                ("Snap latest/edge（較新、較不穩定）", "latest/edge")]}, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._invoke("update-tool", environment_id=environment["id"], tool=tool, snap_channel=dialog.values()["snap_channel"])

    def inspect_ssh_settings(self) -> None:
        environment = self._selected_record("environments")
        if not environment or environment.get("kind") != "ssh":
            self._notice("請先選取 SSH 環境。")
            return
        self._invoke("ssh-settings", environment_id=environment["id"])

    def lock_ssh_password(self) -> None:
        environment = self._selected_record("environments")
        if not environment or environment.get("kind") != "ssh":
            self._notice("請先選取 SSH 環境。")
            return
        try:
            from .askpass import revoke_password
            revoke_password(environment["id"], self.store.path.parent)
            self._toast("SSH 密碼已立即鎖定；下次連線需重新驗證。")
        except OSError as exc:
            self._error("無法鎖定 SSH 密碼", exc)

    def delete_record(self, kind: str) -> None:
        record = self._selected_record(kind)
        if not record:
            return
        if kind == "environments" and record.get("kind") == "local":
            self._notice("內建本機環境不可刪除。")
            return
        extra = ("這會一併清除此環境已保存的 SSH 密碼。" if kind == "environments" and record.get("kind") == "ssh" else
                 "只移除 Launcher 紀錄；保留官方 profile 與憑證。" if kind in {"accounts", "github_accounts"} else "此操作無法復原。")
        title = "刪除環境" if kind == "environments" else "刪除資料"
        if not self._confirm(title, f"確定刪除「{_value(record, 'name')}」？{extra}", confirm_text="刪除", danger=True):
            return
        if kind == "environments" and record.get("kind") == "ssh":
            try:
                from .askpass import revoke_password
                revoke_password(record["id"], self.store.path.parent)
            except Exception as exc:
                self._error("無法撤銷 SSH 密碼", exc)
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
            self._notice("請先選取帳號。")
            return
        if account.get("tool") == "deepseek-harness":
            self.harness_settings(account=account)
            return
        environments = self._records.get("environments", [])
        if not environments:
            self._notice("請先新增執行環境。")
            return
        api_account = _value(account, "auth") == "api"
        method_choices = ([(("API Key（在終端輸入）"), "api")] if api_account else
                          [("Device code（終端驗證）", "device"), ("瀏覽器登入", "browser")])
        bound = "" if api_account else _value(account, "environment_id")
        fields = ([] if bound else [("environment_id", "登入環境", "")]) + [("login_method", "登入方式", "")]
        choices = {"environment_id": self._choice_list("environments"), "login_method": method_choices}
        default_method = "api" if api_account else "device"
        title = "官方 CLI 登入" + (f"（{_value(self._by_id('environments', bound), 'name', bound)}）" if bound else "")
        dialog = RecordDialog(title, fields, {"login_method": default_method}, choices, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        environment = self._by_id("environments", bound or dialog.values()["environment_id"])
        method = dialog.values()["login_method"]
        if _value(account, "tool") == "claude":
            method = "api" if api_account else "browser"
        if method == "browser" and environment.get("kind") == "ssh":
            self._toast("登入在 SSH 終端進行：依終端提示於瀏覽器完成驗證與 callback。", "info")
        elif method == "api":
            self._toast("在終端遮罩輸入 API key；本機使用 Windows Credential Manager，Linux 遠端使用受限檔案保存。", "info")
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
        dialog = RecordDialog("更新工作狀態", [("environment_id", "環境", "")], {}, choices, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._invoke("refresh", environment_id=dialog.values()["environment_id"])

    def export_config(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "匯出設定", "vibe-launcher-config.json", "JSON 設定 (*.json)")
        if path:
            self._run_store(lambda: self.store.export_config(Path(path)), "設定已匯出。")

    def import_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "匯入設定", "", "JSON 設定 (*.json)")
        if path and self._confirm("匯入設定", "匯入內容會更新設定資料，是否繼續？", confirm_text="匯入"):
            self._run_store(lambda: self.store.import_config(Path(path)), "設定已匯入。")

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

    def _start_operation(self, action: str, kwargs: dict, callback) -> None:
        try:
            operation_id = self.service.start(action, **kwargs, spawn=self.workspace.spawn if self._embedded_enabled() else None)
            if not operation_id:
                raise RuntimeError("操作沒有回傳識別碼")
            self._operations[operation_id] = {"callback": callback, "since": time.monotonic(), "action": action,
                                              "login": action == "login" or (action == "github" and kwargs.get("operation", {}).get("github_operation") == "login"),
                                              "embedded": self._embedded_enabled()}
            if self._operations[operation_id]["login"] and self._operations[operation_id]["embedded"]:
                self.show_page("workspace")
            self._toast("已啟動操作；請在終端完成需要的官方驗證。")
        except Exception as exc:
            self._error("無法啟動操作", exc)
            callback({"status": "failed", "error": str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__})

    def _poll_operations(self) -> None:
        for operation_id, info in list(self._operations.items()):
            path = self.store.path.parent / "results" / (operation_id + ".json")
            if path.exists():
                payload = json.loads(runtime_module.read_private(path, limit=65536))
                if payload.get("operation_id") != operation_id or payload.get("action") != info["action"] or not isinstance(payload.get("result"), dict):
                    raise ValueError("操作結果識別不符")
                if info.get("login") and "runner_exit_code" not in payload:
                    if time.monotonic() - info["since"] > 900:
                        del self._operations[operation_id]
                        info["callback"]({"status": "unknown", "message": "runner 尚未完成；請查看登入終端。"})
                    continue
                del self._operations[operation_id]
                path.unlink()
                if info.get("login"):
                    self._login_result(operation_id, payload["result"] if payload["runner_exit_code"] == 0 else {"status": "failed"}, info.get("embedded", False))
                info["callback"](payload["result"])
            elif time.monotonic() - info["since"] > 900:
                del self._operations[operation_id]
                info["callback"]({"status": "unknown", "message": "操作尚未回報；請查看終端並按需重新檢查，遠端工作可能仍執行。"})

    def _login_result(self, operation_id: str, result: dict, embedded: bool) -> None:
        if embedded:
            self.workspace.confirm_login(operation_id, result.get("status", "unknown"))
        elif result.get("status") in {"authenticated", "configured"}:
            self._toast("設定完成" if result["status"] == "configured" else "登入成功")

    def import_profile(self, kind: str) -> None:
        account = self._selected_record("accounts")
        if not account or account.get("tool") == "deepseek-harness":
            self._notice("請選擇 Codex 或 Claude 帳號；Harness 的設定與對話由官方 Web 管理。")
            return
        environment_id = account.get("environment_id") or self.quota_panel.selected_environment() or "local"
        if not self._confirm("明確匯入", "從執行環境的個人 CLI profile 匯入" + ("設定與擴充" if kind == "settings" else "既有對話") + "。只新增或更新仍由 Launcher 管理的內容，保留個人修改及既有對話；不匯入登入憑證。", confirm_text="匯入"):
            return
        self._start_operation("import-" + kind, {"account_id": account["id"], "environment_id": environment_id},
                              lambda result: self._notice("匯入結果：" + json.dumps(result, ensure_ascii=False)))

    # ------------------------------------------------------------------ service plumbing
    # -- account quota ---------------------------------------------------------------------------
    def _invoke(self, action: str, **kwargs: Any) -> bool:
        embedded = action in self.EMBEDDED_ACTIONS and self._embedded_enabled()
        try:
            if embedded:
                try:
                    operation_id = self.service.start(action, spawn=self.workspace.spawn, **kwargs)
                except Exception as exc:
                    if isinstance(exc, (ValueError, KeyError)):
                        raise
                    self._toast("內建終端無法啟動，改用外部終端視窗。", "warn")
                    embedded = False
                    operation_id = self.service.start(action, **kwargs)
            else:
                operation_id = self.service.start(action, **kwargs)
        except Exception as exc:
            self._error("無法啟動終端工作", exc)
            return False
        if embedded:
            self.show_page("workspace")
        if action == "login" and operation_id:
            self._operations[operation_id] = {"callback": lambda result: self.reload(), "since": time.monotonic(),
                                              "action": action, "login": True, "embedded": embedded}
        self._toast("已在工作區開啟終端。" if embedded else "已交由官方 CLI 終端執行。")
        self._mark_pending(action, kwargs)
        self._render_environments()  # show the checking state immediately, not on the next poll
        later(self, 250, self.reload)
        return True

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
                        name = _value(self._by_id("environments", env_id), "name", "環境")
                        self._notice(f"「{name}」的檢查在 {info['ttl'] // 60} 分鐘內沒有回報結果。請查看該終端的輸出，或稍後再試一次。")
            if not self._pending[env_id]:
                del self._pending[env_id]

    def _announce_result(self, env_id: str, kind: str) -> None:
        name = _value(self._by_id("environments", env_id), "name", "環境")
        if kind in {"doctor", "ssh"}:
            self._toast(f"「{name}」的環境檢查已完成。" if kind == "doctor" else f"「{name}」的 SSH 設定已解析。")
            return
        reports = {r.tool: r for r in env_state.load_tool_reports(self.store.path.parent, env_id)}
        if kind == "tools":
            behind = [env_state.TOOL_LABEL[t] for t, r in reports.items() if r.status == "update_available"]
            self._toast(f"CLI 版本檢查完成；{'、'.join(behind)} 有可用更新。" if behind else "CLI 版本檢查完成。", "warn" if behind else "ok")
            return
        tool = kind.split(":", 1)[1]
        report = reports.get(tool)
        if report is not None and report.status == "update_failed":
            self._toast(f"{env_state.TOOL_LABEL.get(tool, tool)} 更新未完成。", "danger")
        else:
            self._toast(f"{env_state.TOOL_LABEL.get(tool, tool)} 更新流程已結束。")

    def _save(self, kind: str, record: dict[str, Any]) -> bool:
        return self._run_store(lambda: self.store.save(kind, record), "已儲存。")

    def _delete(self, kind: str, record_id: str) -> bool:
        return self._run_store(lambda: self.store.delete(kind, record_id), "已刪除。")

    def _run_store(self, operation: Any, success: str) -> bool:
        try:
            operation()
            self.reload()
            self._toast(success)
            return True
        except Exception as exc:
            self._error("資料操作失敗", exc)
            return False

    def _new_id(self) -> str:
        from uuid import uuid4
        return str(uuid4())


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
