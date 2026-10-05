"""工作區: several terminals, organised. Tabs or side-by-side, grouped by project, one click to open a project's
Codex / Claude / plain SSH terminal, and a way to leave a terminal while the work keeps running on the server.

Each terminal is its own SSH connection (no tmux panes inside one connection), so projects on different hosts mix
freely in one workspace.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QScrollArea,
    QSizePolicy, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QVBoxLayout, QWidget,
)

from . import env_state, icons, runtime
from .composer import ComposerBar
from .drafts import FormDraft
from .tag_bar import TagFilterBar, collect_tags, matches_tags, project_tags
from .kit import (
    ActionMenu, ConfirmDialog, ElidedLabel, EmptyState, FilterBar, FlowLayout, IconButton, MenuAction, PageHeader, SearchField,
    StatusBadge, dispose_widget, later, make_button,
)
from .theme import ACCENT_SUBTLE_A, C, R, S, T, TONES, TOOL_ICONS, font, qcolor, section_hue, tool_hue

BADGE_ROLE = int(Qt.ItemDataRole.UserRole) + 1
SUB_ROLE = int(Qt.ItemDataRole.UserRole) + 2
KIND_ROLE = int(Qt.ItemDataRole.UserRole) + 3
PRESET_ROLE = int(Qt.ItemDataRole.UserRole) + 4
GLYPH_ROLE = int(Qt.ItemDataRole.UserRole) + 5
DETACH_GRACE_MS = 4000
TOOL_LABEL = {"codex": "Codex", "claude": "Claude", "shell": "SSH 終端"}
# One-shot jobs that report their result elsewhere (a dialog, the environment cards, a toast). A clean exit closes
# their terminal; a failure keeps it so the output can be read.
BACKGROUND_ACTIONS = {"github", "harness", "import-settings", "import-history", "doctor", "ssh-settings", "check-tools",
                      "refresh", "stop", "quota"}


@dataclass
class TabInfo:
    """What a terminal is for. Enough to label it, group it, save it into a workspace and reopen it."""

    id: str
    title: str
    project: str = ""
    environment: str = ""
    kind: str = "agent"            # agent | shell | login | attach
    tool: str = ""                 # codex | claude | shell
    preset_id: str = ""
    account_id: str = ""
    environment_id: str = ""
    session_id: str = ""
    tmux: bool = False             # the server keeps the work in tmux, so the terminal can be left without ending it
    local: bool = False
    account_name: str = ""
    operation_id: str = ""
    login_operation: bool = False
    action: str = ""

    @property
    def background(self) -> bool:
        return self.action in BACKGROUND_ACTIONS and not self.login_operation

    @property
    def label(self) -> str:
        return " · ".join(filter(None, (self.title, self.account_name)))


@dataclass
class Tab:
    info: TabInfo
    pane: "TerminalPane"
    exit_code: int | None = None
    closing: str = ""              # "" | "detach" | "close"
    attached: bool = False         # the server's tmux told us (via its title) that we are attached to the session
    login_status: str | None = None

    @property
    def terminal(self):
        return self.pane.terminal

    def alive(self) -> bool:
        return self.exit_code is None and self.terminal.is_running()


# --------------------------------------------------------------------------- pane


class TerminalPane(QFrame):
    """A terminal plus its header (status, title, switch / leave / close) and an 'ended' bar."""

    clicked = Signal(str)
    detach_requested = Signal(str)
    close_requested = Signal(str)
    switch_requested = Signal(str, object)
    reopen_requested = Signal(str)
    chord = Signal(str)

    def __init__(self, info: TabInfo, terminal: QWidget, parent: QWidget | None = None):
        super().__init__(parent)
        self.info = info
        self.terminal = terminal
        self._active = False
        self.setObjectName("pane")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        head = QWidget()
        head.setFixedHeight(38)
        row = QHBoxLayout(head)
        row.setContentsMargins(S.md, 0, S.xs + 2, 0)
        row.setSpacing(S.sm)
        self.badge = StatusBadge("", "unknown", "dot", max_width=140)
        row.addWidget(self.badge)
        self.title = ElidedLabel(info.label, px=T.body, weight=QFont.Weight.DemiBold)
        row.addWidget(self.title)
        self.meta = ElidedLabel(" · ".join(filter(None, (info.project, info.environment))), px=T.meta, color="text_muted")
        row.addWidget(self.meta, 1)
        self.composer_button = IconButton("keyboard", "輸入列：在原生輸入框打字（中文輸入法更順手）後送出（Ctrl+Shift+E）",
                                          size=28, icon_size=15)
        self.switch_button = IconButton("repeat", "同專案改開其他終端或環境", size=28, icon_size=15)
        self.detach_button = IconButton("log-out", "離開並讓工作留在伺服器背景執行（Ctrl+Shift+D）", size=28, icon_size=15)
        self.close_button = IconButton("x", "關閉此終端（Ctrl+Shift+W）", size=28, icon_size=15)
        for button in (self.composer_button, self.switch_button, self.detach_button, self.close_button):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            row.addWidget(button)
        self.composer_button.clicked.connect(lambda: self.set_composer_visible(not self.composer_visible(), focus=True))
        self.switch_button.clicked.connect(lambda: self.switch_requested.emit(info.id, self.switch_button))
        self.detach_button.clicked.connect(lambda: self.detach_requested.emit(info.id))
        self.close_button.clicked.connect(lambda: self.close_requested.emit(info.id))
        root.addWidget(head)

        self.ended_bar = QFrame()
        self.ended_bar.setObjectName("endedBar")
        bar = QHBoxLayout(self.ended_bar)
        bar.setContentsMargins(S.md, S.xs + 2, S.md, S.xs + 2)
        bar.setSpacing(S.sm)
        self.ended_label = QLabel()
        self.ended_label.setProperty("role", "muted")
        bar.addWidget(self.ended_label, 1)
        self.reopen_button = make_button("重新開啟", "subtle", "refresh", small=True)
        self.reopen_button.clicked.connect(lambda: self.reopen_requested.emit(info.id))
        bar.addWidget(self.reopen_button)
        self.ended_bar.hide()
        root.addWidget(self.ended_bar)

        root.addWidget(terminal, 1)

        self.composer = ComposerBar()
        self.composer.hide()
        self.composer.submitted.connect(self._submit)
        self.composer.raw.connect(lambda data: self.terminal.send_bytes(data))
        self.composer.chord.connect(self._composer_chord)
        self.composer.focused.connect(lambda: self.clicked.emit(info.id))
        root.addWidget(self.composer)
        self.setProperty("active", False)

    # -- input bar ------------------------------------------------------------------------------
    def composer_visible(self) -> bool:
        return not self.composer.isHidden()

    def set_composer_visible(self, visible: bool, *, focus: bool = False) -> None:
        self.composer.setVisible(visible)
        self.composer_button.active_color = "accent" if visible else None
        self.composer_button.update()
        if focus:
            self.focus_input()

    def focus_input(self) -> None:
        """Keyboard to the input bar when it is shown, else to the terminal."""
        if self.composer_visible():
            self.composer.focus()
        else:
            self.terminal.focus_terminal()

    def _submit(self, text: str) -> None:
        submit = getattr(self.terminal, "submit", None)
        if submit is not None:
            submit(text)

    def _composer_chord(self, name: str) -> None:
        if name == "composer":       # from the input bar, the same keys go back to the terminal
            self.terminal.focus_terminal()
        else:
            self.chord.emit(name)

    def set_active(self, active: bool) -> None:
        # Styled by the application stylesheet (QFrame#pane[active]). Only re-polish this frame, and only on a real
        # change: restyling a pane re-polishes its whole subtree, web view included, which made terminals flash.
        if active == self._active:
            return
        self._active = active
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def set_status(self, label: str, tone: str) -> None:
        self.badge.set_state(label, tone, "dot", "")

    def show_ended(self, text: str, can_reopen: bool) -> None:
        self.ended_label.setText(text)
        self.reopen_button.setVisible(can_reopen)
        self.ended_bar.show()
        self.set_composer_visible(False)
        self.composer_button.setEnabled(False)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit(self.info.id)
        super().mousePressEvent(event)


# --------------------------------------------------------------------------- area


class TerminalArea(QWidget):
    """Places panes by hand (so web views are never reparented): one at a time as tabs, or all side by side."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.mode = "tabs"
        self.panes: list[TerminalPane] = []
        self.active: TerminalPane | None = None
        self.gap = S.sm

    def add(self, pane: TerminalPane) -> None:
        pane.setParent(self)
        self.panes.append(pane)
        self.relayout()

    def remove(self, pane: TerminalPane) -> None:
        if pane in self.panes:
            self.panes.remove(pane)
        if self.active is pane:
            self.active = self.panes[-1] if self.panes else None
        self.relayout()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.relayout()

    def set_active(self, pane: TerminalPane | None) -> None:
        self.active = pane
        self.relayout()

    @staticmethod
    def grid_shape(count: int) -> tuple[int, int]:
        """(columns, rows) for ``count`` side-by-side terminals."""
        if count <= 1:
            return 1, 1
        columns = 2 if count <= 4 else 3
        return columns, -(-count // columns)

    def pane_rects(self) -> dict[TerminalPane, QRect]:
        width, height = self.width(), self.height()
        if not self.panes:
            return {}
        if self.mode == "tabs" or len(self.panes) == 1:
            return {self.active or self.panes[0]: QRect(0, 0, width, height)}
        columns, rows = self.grid_shape(len(self.panes))
        cell_w = (width - self.gap * (columns - 1)) // columns
        cell_h = (height - self.gap * (rows - 1)) // rows
        return {pane: QRect((i % columns) * (cell_w + self.gap), (i // columns) * (cell_h + self.gap), cell_w, cell_h)
                for i, pane in enumerate(self.panes)}

    def relayout(self) -> None:
        """Place every pane. Panes not on show are parked just outside the area, at full size, instead of hidden.

        Hiding a web view makes Chromium drop its frames and resizing it makes the program in the terminal redraw
        everything; either one flashes when the pane comes back. A parked pane keeps both, so switching is instant.
        """
        rects = self.pane_rects()
        parked = QRect(-self.width() - 64, 0, self.width(), self.height())
        for pane in self.panes:
            rect = rects.get(pane)
            if rect is None:
                rect = parked
            if pane.geometry() != rect:
                pane.setGeometry(rect)
            if pane.isHidden():
                pane.show()
            pane.set_active(self.mode == "grid" and len(self.panes) > 1 and pane is self.active)
        if self.active is not None:
            self.active.raise_()

    def on_screen(self, pane: "TerminalPane") -> bool:
        return not pane.isHidden() and self.rect().intersects(pane.geometry())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.relayout()


# --------------------------------------------------------------------------- rail


def draw_pill(painter: QPainter, right: float, center_y: float, text: str, tone: str) -> float:
    """Right-aligned status pill; returns the width it used."""
    pill_font = font(T.meta, QFont.Weight.Medium)
    width = QFontMetrics(pill_font).horizontalAdvance(text) + 14 + 12 + 6
    pill = QRectF(right - width, center_y - 11, width, 22)
    fg = TONES.get(tone, C.neutral)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(qcolor(C.border if tone == "unknown" else fg, 0.14))
    painter.drawRoundedRect(pill, 11, 11)
    painter.setBrush(QColor(fg))
    painter.drawEllipse(QRectF(pill.left() + 9, pill.center().y() - 3, 6, 6))
    painter.setFont(pill_font)
    painter.setPen(QColor(C.text_secondary if tone == "unknown" else fg))
    painter.drawText(QRectF(pill.left() + 20, pill.top(), width - 24, pill.height()),
                     Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
    return width


class RailDelegate(QStyledItemDelegate):
    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), 30 if index.data(KIND_ROLE) == "header" else 52)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(2, 1, -2, -1)
        if index.data(KIND_ROLE) == "header":
            painter.setFont(font(T.micro, QFont.Weight.DemiBold))
            painter.setPen(QColor(C.text_muted))
            painter.drawText(QRectF(rect.left() + 10, rect.top() + 8, rect.width() - 14, 18),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, str(index.data(Qt.ItemDataRole.DisplayRole)))
            painter.restore()
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.setPen(Qt.PenStyle.NoPen)
        if selected:
            painter.setBrush(qcolor(C.accent, ACCENT_SUBTLE_A))
            painter.drawRoundedRect(rect, R.md, R.md)
            painter.setBrush(qcolor(C.accent))
            painter.drawRoundedRect(QRectF(rect.left(), rect.center().y() - 9, 3, 18), 1.5, 1.5)
        elif hovered:
            painter.setBrush(qcolor(C.border, 0.08))
            painter.drawRoundedRect(rect, R.md, R.md)
        glyph = index.data(GLYPH_ROLE) or "terminal"
        painter.drawPixmap(round(rect.left() + 12), round(rect.center().y() - 8),
                           icons.pixmap(glyph, C.accent if selected else C.text_secondary, 16, 2.0))
        left = rect.left() + 38
        width = rect.width() - 38 - 10
        badge = index.data(BADGE_ROLE)
        if badge:
            width -= draw_pill(painter, rect.right() - 8, rect.center().y(), badge[0], badge[1]) + 8
        title_font = font(T.body, QFont.Weight.Medium)
        painter.setFont(title_font)
        painter.setPen(QColor(C.text))
        painter.drawText(QRectF(left, rect.top() + 6, width, 20), Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(title_font).elidedText(str(index.data(Qt.ItemDataRole.DisplayRole)), Qt.TextElideMode.ElideRight, int(width)))
        sub_font = font(T.meta)
        painter.setFont(sub_font)
        painter.setPen(QColor(C.text_muted))
        painter.drawText(QRectF(left, rect.top() + 26, width + 60, 18), Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(sub_font).elidedText(str(index.data(SUB_ROLE) or ""), Qt.TextElideMode.ElideRight, int(width + 60)))
        painter.restore()


class TerminalRail(QListWidget):
    """Open terminals grouped by project; arrow keys / click to switch."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setItemDelegate(RailDelegate(self))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAccessibleName("已開啟的終端")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def rows(self) -> list[tuple[str, str]]:
        """[(kind, id-or-title)] in display order, for tests and for change detection."""
        out = []
        for row in range(self.count()):
            item = self.item(row)
            out.append(("header", item.text()) if item.data(KIND_ROLE) == "header" else ("tab", item.data(Qt.ItemDataRole.UserRole)))
        return out


# --------------------------------------------------------------------------- project launcher


class ProjectLauncher(QWidget):
    """One row per saved project with Codex / Claude / SSH buttons: the fastest way into work."""

    launch = Signal(str, str, str, str)   # preset_id, kind (agent|shell), account_id, environment_id
    tags_changed = Signal(list)           # the tag filter the user chose (remembered between runs)
    tool_menu = Signal(str, str, object)  # preset_id, tool, anchor: "which account for this tool?"
    restore = Signal(str)                 # preset_id: bring all of its workspaces back

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._projects: list[dict] = []
        self._accounts: list[dict] = []
        self._environments: list[dict] = []
        self._signature: tuple | None = None
        self.recoverable: dict[str, int] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(S.sm)
        title = QLabel("快速開啟")
        title.setProperty("role", "section")
        title.setStyleSheet(f"font-size: {T.body + 1}px;")
        root.addWidget(title)
        self.search = SearchField("搜尋專案")
        self.search.textChanged.connect(lambda _: self._rebuild(force=True))
        root.addWidget(self.search)
        self.tag_bar = TagFilterBar()
        self.tag_bar.changed.connect(self._tags_clicked)
        root.addWidget(self.tag_bar)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.host = QWidget()
        self.rows_layout = QVBoxLayout(self.host)
        self.rows_layout.setContentsMargins(0, 0, S.xs, 0)
        self.rows_layout.setSpacing(S.xs)
        self.scroll.setWidget(self.host)
        root.addWidget(self.scroll, 1)
        self.hint = QLabel("還沒有專案。先到「專案」頁新增，之後就能在這裡一鍵開啟。")
        self.hint.setProperty("role", "meta")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

    def fitting(self, environment_id: str, tool: str = "") -> list[dict]:
        """Accounts usable on ``environment_id`` (a subscription only where it is signed in)."""
        return [a for a in self._accounts if runtime.account_fits(a, environment_id or "local") and (not tool or a.get("tool") == tool)]

    def tool_account(self, project: dict, tool: str, environment_id: str = "") -> dict | None:
        """The account to use when opening ``project`` with ``tool`` there: the last one used if it fits, else the first that does."""
        pool = self.fitting(environment_id or project.get("environment_id", ""), tool)
        mine = next((a for a in pool if a.get("id") == project.get("account_id")), None)
        return mine or (pool[0] if pool else None)

    def set_tag_filter(self, tags: list[str]) -> None:
        self.tag_bar.set_selected(tags)
        self._rebuild(force=True)

    def _tags_clicked(self, tags: list) -> None:
        self.tags_changed.emit(list(tags))
        self._rebuild(force=True)

    def set_recoverable(self, counts: dict[str, int]) -> None:
        """How many workspaces of each project could be brought back (shown as a button on its card)."""
        counts = {k: v for k, v in counts.items() if v}
        if counts != self.recoverable:
            self.recoverable = counts
            self._rebuild()

    def set_data(self, projects: list[dict], accounts: list[dict], environments: list[dict]) -> None:
        self._projects, self._accounts, self._environments = projects, accounts, environments
        if self.tag_bar.set_tags(collect_tags(projects)):
            self.tags_changed.emit(self.tag_bar.selected())
        self._rebuild()

    def visible_projects(self) -> list[dict]:
        term = self.search.text().strip().casefold()
        projects = [p for p in self._projects if term in f"{p.get('name', '')} {p.get('path', '')}".casefold()
                    and matches_tags(p, self.tag_bar.selected())]

        def order(project: dict):
            stamp = env_state.parse_time(str(project.get("last_used") or ""))
            return (not project.get("favorite"), -(stamp.timestamp() if stamp else 0.0), str(project.get("name", "")).casefold())
        return sorted(projects, key=order)

    def _rebuild(self, force: bool = False) -> None:
        projects = self.visible_projects()
        signature = (tuple((p.get("id"), p.get("name"), p.get("favorite"), p.get("account_id"), p.get("environment_id"), tuple(project_tags(p))) for p in projects),
                     tuple((a.get("id"), a.get("tool"), a.get("name"), a.get("auth"), a.get("environment_id")) for a in self._accounts),
                     tuple((e.get("id"), e.get("name")) for e in self._environments),
                     tuple(sorted(self.recoverable.items())))
        if signature == self._signature and not force:
            return
        self._signature = signature
        while self.rows_layout.count():
            widget = self.rows_layout.takeAt(0).widget()
            if widget is not None:
                dispose_widget(widget)
        for project in projects:
            self.rows_layout.addWidget(self._row(project))
        self.rows_layout.addStretch(1)
        self.hint.setText("還沒有專案。先到「專案」頁新增，之後就能在這裡一鍵開啟。" if not self._projects
                          else "沒有符合的專案。調整搜尋或標籤篩選。")
        self.hint.setVisible(not projects)

    def _row(self, project: dict) -> QWidget:
        env = next((e for e in self._environments if e.get("id") == project.get("environment_id")), {})
        row = QFrame()
        row.setProperty("surface", "card")
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        box = QVBoxLayout(row)
        box.setContentsMargins(S.md, S.sm + 2, S.sm, S.sm + 2)
        box.setSpacing(S.xs + 2)
        top = QHBoxLayout()
        top.setSpacing(S.xs)
        name = ElidedLabel(("★ " if project.get("favorite") else "") + str(project.get("name", "")), px=T.body, weight=QFont.Weight.DemiBold)
        top.addWidget(name, 1)
        box.addLayout(top)
        tags = " ".join("#" + t for t in project_tags(project))
        box.addWidget(ElidedLabel(str(env.get("name", "")) + (f"  ·  {tags}" if tags else ""), px=T.meta, color="text_muted"))
        buttons_host = QWidget()                     # wraps onto a second line when the side column is narrow
        buttons = FlowLayout(buttons_host, S.xs + 2, S.xs + 2)
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(S.xs + 2)
        for tool in ("codex", "claude"):
            account = self.tool_account(project, tool)
            choices = self.fitting(project.get("environment_id", ""), tool)
            # One split button per service: the label opens it with the default account, the arrow picks another.
            group = QWidget()
            pair = QHBoxLayout(group)
            pair.setContentsMargins(0, 0, 0, 0)
            pair.setSpacing(1)
            button = make_button(TOOL_LABEL[tool], "subtle", TOOL_ICONS[tool], small=True, tint=tool_hue(tool),
                                 tip=(f"以帳號「{account.get('name')}」開啟" + ("（▾ 可換帳號）" if len(choices) > 1 else ""))
                                 if account else f"這個環境沒有可用的 {TOOL_LABEL[tool]} 帳號（訂閱帳號要綁定此環境）")
            button.setEnabled(account is not None)
            button.clicked.connect(lambda _=False, pid=project["id"], aid=(account or {}).get("id", ""): self.launch.emit(pid, "agent", aid, ""))
            pair.addWidget(button)
            arrow = make_button("", "subtle", "chevron-down", small=True, tip=f"選擇要用哪個帳號開啟 {TOOL_LABEL[tool]}")
            arrow.setAccessibleName(f"選擇 {TOOL_LABEL[tool]} 帳號")
            arrow.setEnabled(bool(choices))
            arrow.setFixedWidth(26)
            arrow.clicked.connect(lambda _=False, pid=project["id"], t=tool, b=arrow: self.tool_menu.emit(pid, t, b))
            pair.addWidget(arrow)
            buttons.addWidget(group)
        ssh = make_button("SSH", "subtle", "square-terminal", small=True, tint="accent", tip="只連線並進入專案資料夾，不執行 Codex 或 Claude")
        ssh.clicked.connect(lambda _=False, pid=project["id"]: self.launch.emit(pid, "shell", "", ""))
        buttons.addWidget(ssh)
        waiting = self.recoverable.get(project["id"], 0)
        if waiting:
            back = make_button(f"召回 {waiting}", "subtle", "repeat", small=True,
                               tip=f"一次召回這個專案的 {waiting} 個工作區：接回還在執行的，重新開啟已停止的")
            back.setAccessibleName(f"召回 {project.get('name', '')} 的全部工作區")
            back.clicked.connect(lambda _=False, pid=project["id"]: self.restore.emit(pid))
            buttons.addWidget(back)
        box.addWidget(buttons_host)
        return row


# --------------------------------------------------------------------------- page


class WorkspacePage(QWidget):
    launch_requested = Signal(str, str, str, str)   # preset_id, kind, account_id, environment_id
    palette_requested = Signal()
    save_workspace_requested = Signal()
    open_workspace_requested = Signal(str)
    delete_workspace_requested = Signal(str)
    layout_changed = Signal(str)
    notify = Signal(str, str)                       # message, tone
    terminals_changed = Signal()
    restore_requested = Signal(str)                 # preset_id: bring all of that project's workspaces back

    def __init__(self, terminal_factory: Callable[[], QWidget] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._factory = terminal_factory or self._default_factory
        self.composer_default = True       # Codex / Claude terminals open with the input bar shown (UI preference)
        self.draft_root: Path | None = None   # where unsent input-bar text is kept (the launcher data folder)
        self._tabs: dict[str, Tab] = {}
        self._order: list[str] = []
        self._active: str | None = None
        self._projects: list[dict] = []
        self._accounts: list[dict] = []
        self._environments: list[dict] = []
        self._sessions: dict[str, dict] = {}
        self._workspaces: list[dict] = []
        self._confirm: Callable[..., bool] = lambda title, message, **kw: ConfirmDialog.ask(self, title, message, **kw)

        root = QVBoxLayout(self)
        root.setSpacing(S.lg)
        self.root = root
        self.header = PageHeader("工作區", "同時管理多個專案與 SSH 終端。離開或切換終端不會中斷伺服器上背景執行的工作。",
                                   icon="square-terminal", hue=section_hue("workspace"))
        self.layout_bar = FilterBar([("tabs", "分頁"), ("grid", "並排")])
        self.layout_bar.changed.connect(self.set_layout)
        self.workspace_button = make_button("工作區", "secondary", "briefcase", tip="開啟或儲存一組終端排列")
        self.workspace_button.clicked.connect(self._workspace_menu)
        self.palette_button = make_button("Ctrl K", "ghost", "command", tip="指令面板：快速切換終端")
        self.palette_button.clicked.connect(self.palette_requested)
        self.header.add_action(self.layout_bar)
        self.header.add_action(self.workspace_button)
        self.header.add_action(self.palette_button)
        root.addWidget(self.header)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(S.lg)
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(S.lg)
        rail_title = QLabel("終端")
        rail_title.setProperty("role", "section")
        rail_title.setStyleSheet(f"font-size: {T.body + 1}px;")
        rail_heading = QHBoxLayout()
        rail_heading.addWidget(rail_title, 1)
        self.move_up_button = make_button("↑", "ghost", small=True, tip="將選取的終端往前移一格")
        self.move_down_button = make_button("↓", "ghost", small=True, tip="將選取的終端往後移一格")
        self.move_up_button.setAccessibleName("終端往前移")
        self.move_down_button.setAccessibleName("終端往後移")
        self.move_up_button.clicked.connect(lambda: self.move_tab(self._active, -1))
        self.move_down_button.clicked.connect(lambda: self.move_tab(self._active, 1))
        rail_heading.addWidget(self.move_up_button)
        rail_heading.addWidget(self.move_down_button)
        side_layout.addLayout(rail_heading)
        self.rail = TerminalRail()
        self.rail.currentItemChanged.connect(self._rail_changed)
        self.rail.customContextMenuRequested.connect(self._rail_menu)
        self.rail_hint = QLabel("還沒有開啟的終端。從下方「快速開啟」選一個專案。")
        self.rail_hint.setProperty("role", "meta")
        self.rail_hint.setWordWrap(True)
        side_layout.addWidget(self.rail_hint)
        side_layout.addWidget(self.rail, 3)
        self.rail.hide()                          # an empty list would only waste the room the project launcher can use
        self.launcher = ProjectLauncher()
        self.launcher.launch.connect(self.launch_requested)
        self.launcher.tool_menu.connect(self._tool_menu)
        self.launcher.restore.connect(self.restore_requested)
        self.terminals_changed.connect(self._update_recoverable)
        side_layout.addWidget(self.launcher, 4)
        side.setMinimumWidth(240)
        side.setMaximumWidth(440)
        self.splitter.addWidget(side)

        self.area = TerminalArea()
        self.empty = EmptyState()
        self.empty.set_content("square-terminal", "還沒有開啟的終端",
                               "從左側「快速開啟」選一個專案：用 Codex、Claude 開始工作，或只用 SSH 進入專案資料夾。")
        self.right = QStackedWidget()
        self.right.addWidget(self.empty)
        self.right.addWidget(self.area)
        self.splitter.addWidget(self.right)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([340, 860])
        root.addWidget(self.splitter, 1)
        self.set_compact(False)

    # -- factory ----------------------------------------------------------------------------
    @staticmethod
    def _default_factory() -> QWidget:
        from .terminal import TerminalWidget
        return TerminalWidget()

    def set_compact(self, compact: bool) -> None:
        m = S.lg if compact else S.xl
        self.root.setContentsMargins(m, m, m, m)

    # -- data from the window ---------------------------------------------------------------
    def set_data(self, projects: list[dict], accounts: list[dict], environments: list[dict], workspaces: list[dict]) -> None:
        self._projects, self._accounts, self._environments, self._workspaces = projects, accounts, environments, workspaces
        self.launcher.set_data(projects, accounts, environments)
        for tab in self.tabs():
            account = next((a for a in accounts if a.get("id") == tab.info.account_id), {})
            tab.info.account_name = str(account.get("name") or tab.info.account_id) if tab.info.kind != "shell" else ""
            tab.pane.title.setText(tab.info.label)
        self._rebuild_rail()
        self._update_recoverable()

    def update_sessions(self, sessions: list[dict]) -> None:
        self._sessions = {s["id"]: s for s in sessions if s.get("id")}
        for tab in self._tabs.values():
            self._refresh_tab(tab)
        self._rebuild_rail()
        self._update_recoverable()

    def _update_recoverable(self) -> None:
        open_ids = {t.info.session_id for t in self._tabs.values() if t.info.session_id and t.alive()}
        sessions = list(self._sessions.values())
        self.launcher.set_recoverable({p["id"]: len(env_state.restore_plan(p["id"], sessions, self._environments, self._accounts, open_ids,
                                                                           p.get("environment_id", "")))
                                       for p in self._projects if p.get("id")})

    # -- tabs --------------------------------------------------------------------------------
    def tabs(self) -> list[Tab]:
        return [self._tabs[i] for i in self._order]

    def tab(self, tab_id: str) -> Tab | None:
        return self._tabs.get(tab_id)

    def selected_id(self) -> str | None:
        return self._active

    def running_count(self) -> int:
        return sum(1 for tab in self._tabs.values() if tab.alive())

    def spawn(self, args: list[str], ctx: dict) -> Tab:
        """Run a launcher job (``python -m vibe_launcher.runner job.json``) in a new terminal. Used as ``spawn=`` by the service."""
        info = self._info_from(ctx)
        terminal = self._factory()
        env = dict(os.environ)
        env.update(VIBE_EMBEDDED="1", PYTHONUTF8="1")
        try:
            terminal.start(args, cwd=str(Path.home()), env=env)
        except Exception:
            terminal.dispose()
            raise
        return self.add_terminal(info, terminal)

    def add_terminal(self, info: TabInfo, terminal: QWidget) -> Tab:
        if info.background:
            # Repeating a check replaces its earlier, already finished terminal instead of piling up copies.
            for old in [t for t in self._tabs.values() if t.info.background and not t.alive() and not t.closing
                        and (t.info.action, t.info.title, t.info.account_name, t.info.environment_id)
                        == (info.action, info.title, info.account_name, info.environment_id)]:
                self._remove(old.info.id)
        pane = TerminalPane(info, terminal)
        tab = Tab(info=info, pane=pane)
        self._tabs[info.id] = tab
        self._order.append(info.id)
        terminal.exited.connect(lambda code, i=info.id: self._on_exit(i, code))
        drained = getattr(terminal, "output_drained", None)
        if drained is not None:
            drained.connect(lambda i=info.id: self._finish_login(i) or self._finish_background(i))
        terminal.title_changed.connect(lambda text, i=info.id: self._on_title(i, text))
        terminal.chord.connect(self._on_chord)
        focused = getattr(terminal, "focused", None)   # tied to the terminal's own lifetime, unlike an application-wide signal
        if focused is not None:
            focused.connect(lambda i=info.id: self._follow_focus(i))
        pane.clicked.connect(self.select)
        pane.detach_requested.connect(self.detach)
        pane.close_requested.connect(self.close_tab)
        pane.switch_requested.connect(self._switch_menu)
        pane.reopen_requested.connect(self._reopen)
        pane.chord.connect(self._on_chord)
        restored = False
        if self.draft_root is not None and info.kind in {"agent", "attach"} and (info.preset_id or info.session_id):
            # Unsent input-bar text outlives the terminal: reopening the same project and tool brings it back.
            pane.draft = FormDraft(pane, self.draft_root, f"composer:{info.preset_id or info.session_id}:{info.tool}",
                                   {"composer": pane.composer.edit})
            restored = pane.draft.restored
        pane.set_composer_visible(restored or (self.composer_default and info.kind in {"agent", "attach"}
                                               and info.tool in {"codex", "claude"}))
        if restored:
            self.notify.emit("已還原這個專案上次沒送出的輸入列文字。", "info")
        self.area.add(pane)
        self._refresh_tab(tab)
        self.select(info.id)
        self._rebuild_rail()
        self.terminals_changed.emit()
        return tab

    def _info_from(self, ctx: dict) -> TabInfo:
        session = self._sessions.get(ctx.get("session_id") or "", {})
        preset = next((p for p in self._projects if p.get("id") == (ctx.get("preset_id") or session.get("preset_id"))), {})
        env_id = ctx.get("environment_id") or session.get("environment_id") or preset.get("environment_id", "")
        env = next((e for e in self._environments if e.get("id") == env_id), {})
        account = next((a for a in self._accounts if a.get("id") == (ctx.get("account_id") or session.get("account_id"))), {})
        action = ctx.get("action", "launch")
        mode = ctx.get("mode") or session.get("mode") or "agent"
        kind = "login" if action == "login" else ("attach" if action == "attach" else ("shell" if mode == "shell" else "agent"))
        tool = "shell" if mode == "shell" else account.get("tool", "")
        if kind == "login":
            title = f"登入 {TOOL_LABEL.get(tool, '')}".strip()
        else:
            title = TOOL_LABEL.get(tool, "終端")
        if ctx.get("operation_title"):
            title = ctx["operation_title"]
            kind = "operation"
        persistent = session.get("persistent", preset.get("persistent", True)) is not False
        local = env.get("kind") == "local"
        return TabInfo(
            id=uuid.uuid4().hex, title=title, project=str(preset.get("name") or session.get("name") or ""),
            environment=str(env.get("name", "")), kind=kind, tool=tool, preset_id=str(preset.get("id", "")),
            account_id=str(account.get("id", "")), environment_id=str(env_id), session_id=str(ctx.get("session_id") or ""),
            tmux=bool(persistent and not local and kind in {"agent", "shell", "attach"}), local=local,
            operation_id=str(ctx.get("operation_id") or ""), login_operation=ctx.get("login_operation") is True, action=str(action),
            account_name=str(ctx.get("operation_account_name") or account.get("name") or account.get("id") or "") if kind != "shell" else "")

    def select(self, tab_id: str) -> None:
        tab = self._tabs.get(tab_id)
        if tab is None:
            return
        self._active = tab_id
        self.area.set_active(tab.pane)
        self.right.setCurrentWidget(self.area)
        self._sync_rail_selection()
        later(self, 0, lambda: self._focus_active(tab_id))

    def _focus_active(self, tab_id: str) -> None:
        """Give the keyboard to a terminal, but only if it is still the one being shown.

        A focus request queued by an earlier select() would otherwise run after a later one, steal focus back, and
        (because focusing a terminal selects it) start a tug of war that keeps showing and hiding the panes.
        """
        tab = self._tabs.get(tab_id)
        if tab is not None and tab_id == self._active:
            tab.pane.focus_input()

    def goto(self, number: int) -> None:
        if 1 <= number <= len(self._order):
            self.select(self._order[number - 1])

    def step(self, delta: int) -> None:
        if self._order and self._active in self._order:
            self.select(self._order[(self._order.index(self._active) + delta) % len(self._order)])

    def move_tab(self, tab_id: str | None, delta: int) -> None:
        if tab_id not in self._order:
            return
        index = self._order.index(tab_id)
        target = index + delta
        if not 0 <= target < len(self._order):
            return
        self._order.insert(target, self._order.pop(index))
        self.area.panes[:] = [tab.pane for tab in self.tabs()]
        self.area.relayout()
        self._rebuild_rail()
        self.terminals_changed.emit()

    # -- status -------------------------------------------------------------------------------
    def status_for(self, tab: Tab) -> tuple[str, str]:
        record = self._sessions.get(tab.info.session_id, {})
        if tab.alive():
            return ("已開啟", "ok") if not tab.closing else ("離開中…", "info")
        known = str(record.get("status", ""))
        if known in {"ended", "detached", "failed"}:
            return env_state.session_badge(known)
        if tab.exit_code in (None,):
            return ("已開啟", "ok")
        return ("已結束", "unknown") if tab.exit_code == 0 else ("結束 (%s)" % tab.exit_code, "danger")

    def _refresh_tab(self, tab: Tab) -> None:
        label, tone = self.status_for(tab)
        tab.pane.set_status(label, tone)
        alive = tab.alive()
        pane = tab.pane
        pane.detach_button.setEnabled(alive and tab.info.tmux)
        pane.detach_button.setToolTip("離開並讓工作留在伺服器背景執行（Ctrl+Shift+D）" if tab.info.tmux
                                      else "此終端沒有使用 tmux，離開就會結束工作；切換到其他分頁即可讓它留在後面")
        if tab.exit_code is not None:
            pane.show_ended(self._ended_text(tab), can_reopen=bool(tab.info.preset_id and tab.info.kind in {"agent", "shell"}))

    def _ended_text(self, tab: Tab) -> str:
        known = str(self._sessions.get(tab.info.session_id, {}).get("status", ""))
        if known == "detached":
            return "已離開；工作仍在伺服器背景執行，可從「工作階段」或這裡重新接回。"
        if known == "failed":
            return "終端已結束，工作失敗。上方保留了輸出，方便查看原因。"
        return f"終端已結束（退出碼 {tab.exit_code}）。輸出已保留，可以回頭查看。"

    # -- events -------------------------------------------------------------------------------
    def _on_title(self, tab_id: str, text: str) -> None:
        tab = self._tabs.get(tab_id)
        if tab is not None:
            tab.attached = text.startswith("vibe:")
            self._rebuild_rail()

    def _on_chord(self, name: str) -> None:
        if name == "next":
            self.step(1)
        elif name == "prev":
            self.step(-1)
        elif name.startswith("goto:"):
            self.goto(int(name.split(":", 1)[1]))
        elif name == "palette":
            self.palette_requested.emit()
        elif self._active:
            if name == "detach":
                self.detach(self._active)
            elif name == "close":
                self.close_tab(self._active)
            elif name == "new":
                self._switch_menu(self._active, self.workspace_button)
            elif name == "composer" and self._active in self._tabs and self._tabs[self._active].alive():
                self._tabs[self._active].pane.set_composer_visible(True, focus=True)

    def _on_exit(self, tab_id: str, code: int) -> None:
        tab = self._tabs.get(tab_id)
        if tab is None:
            return
        tab.exit_code = code
        if tab.closing:
            self._remove(tab_id)
            if tab.closing == "detach":
                self.notify.emit(f"已離開「{tab.info.project} · {tab.info.title}」；工作仍在伺服器背景執行。", "ok")
            return
        if self._finish_login(tab_id) or self._finish_background(tab_id):
            return
        self._refresh_tab(tab)
        self._rebuild_rail()
        self.terminals_changed.emit()

    def confirm_login(self, operation_id: str, status: str) -> None:
        """Accept the runner's result for this login, independently of process/output completion."""
        if not operation_id or status not in {"authenticated", "configured"}:
            return
        for tab in list(self._tabs.values()):
            if tab.info.login_operation and tab.info.operation_id == operation_id:
                tab.login_status = status
                self._finish_login(tab.info.id)

    def _finish_login(self, tab_id: str) -> bool:
        tab = self._tabs.get(tab_id)
        if (tab is None or not tab.info.login_operation or not tab.info.operation_id or tab.closing
                or tab.exit_code != 0 or tab.login_status not in {"authenticated", "configured"}
                or not getattr(tab.terminal, "is_output_drained", True)):
            return False
        label = tab.info.account_name or tab.info.title
        message = "設定完成" if tab.login_status == "configured" else "登入成功"
        self._remove(tab_id)
        self.notify.emit(f"「{label}」{message}。", "ok")
        return True

    def _finish_background(self, tab_id: str) -> bool:
        tab = self._tabs.get(tab_id)
        if (tab is None or not tab.info.background or tab.closing or tab.exit_code != 0
                or not getattr(tab.terminal, "is_output_drained", True)):
            return False
        self._remove(tab_id)
        return True

    def _follow_focus(self, tab_id: str) -> None:
        if tab_id != self._active and tab_id in self._tabs:
            self.select(tab_id)

    # -- leave / close ------------------------------------------------------------------------
    def _send_detach(self, tab: Tab, reason: str) -> None:
        tab.closing = reason
        tab.terminal.detach_tmux()
        self._refresh_tab(tab)
        later(self, DETACH_GRACE_MS, lambda: self._force_end(tab.info.id))

    def detach(self, tab_id: str) -> None:
        """Leave the terminal but let the server keep working (tmux detach). Terminals without tmux cannot do that."""
        tab = self._tabs.get(tab_id)
        if tab is None or not tab.alive():
            return
        if not tab.info.tmux:
            self.notify.emit("這個終端沒有使用 tmux，離開就會結束工作。想暫時離開時，切換到其他分頁即可，連線會保留。", "warn")
            return
        if not tab.attached:
            self.notify.emit("還沒連上伺服器的 tmux 工作階段（可能仍在登入驗證）。連上後再按離開。", "warn")
            return
        self._send_detach(tab, "detach")

    def close_tab(self, tab_id: str, *, confirm: bool = True) -> None:
        tab = self._tabs.get(tab_id)
        if tab is None:
            return
        if not tab.alive():
            self._remove(tab_id)
            return
        if tab.info.tmux and tab.attached:
            self._send_detach(tab, "close")      # leave cleanly: the server keeps the work and the state is recorded
            return
        if not tab.info.tmux and confirm and not self._confirm(
                "關閉終端", f"「{tab.info.project} · {tab.info.title}」沒有使用 tmux，關閉會結束其中的工作。",
                confirm_text="關閉並結束", danger=True):
            return
        # not attached to a server-side session yet (still logging in) or no tmux at all
        tab.closing = "close"
        tab.terminal.terminate()

    def _force_end(self, tab_id: str) -> None:
        tab = self._tabs.get(tab_id)
        if tab is not None and tab.alive():
            tab.terminal.terminate()

    def _remove(self, tab_id: str) -> None:
        tab = self._tabs.pop(tab_id, None)
        if tab is None:
            return
        self._order.remove(tab_id)
        self.area.remove(tab.pane)
        tab.terminal.dispose()
        tab.pane.deleteLater()
        if self._active == tab_id:
            self._active = self._order[-1] if self._order else None
            if self._active:
                self.select(self._active)
        if not self._order:
            self.right.setCurrentWidget(self.empty)
        self._rebuild_rail()
        self.terminals_changed.emit()

    def shutdown(self) -> None:
        for tab_id in list(self._order):
            tab = self._tabs[tab_id]
            tab.closing = "close"
            tab.terminal.terminate()
            self._remove(tab_id)

    def _reopen(self, tab_id: str) -> None:
        tab = self._tabs.get(tab_id)
        if tab is not None and tab.info.preset_id:
            self.launch_requested.emit(tab.info.preset_id, "shell" if tab.info.kind == "shell" else "agent",
                                       tab.info.account_id if tab.info.kind == "agent" else "", "")
            self._remove(tab_id)

    # -- rail ---------------------------------------------------------------------------------
    def _rebuild_rail(self) -> None:
        groups: list[tuple[str, list[Tab]]] = []
        for tab in self.tabs():
            project = tab.info.project or "其他"
            if not groups or groups[-1][0] != project:
                groups.append((project, []))
            groups[-1][1].append(tab)
        rows = []
        self.rail._presets = {}
        for project, tabs in groups:
            rows.append(("header", f"{project} · {len(tabs)}"))
            self.rail._presets[f"{project} · {len(tabs)}"] = next((t.info.preset_id for t in tabs if t.info.preset_id), "")
            rows += [("tab", t.info.id) for t in tabs]
        self.rail.setVisible(bool(rows))
        self.rail_hint.setVisible(not rows)
        signature = [(kind, value, self.status_for(self._tabs[value]) if kind == "tab" else None,
                        (self._tabs[value].attached, self._tabs[value].info.account_name) if kind == "tab" else None) for kind, value in rows]
        if signature == getattr(self.rail, "_signature", None):
            return
        self.rail._signature = signature
        self.rail.blockSignals(True)
        self.rail.clear()
        for kind, value in rows:
            item = QListWidgetItem()
            if kind == "header":
                item.setText(value)
                item.setData(KIND_ROLE, "header")
                item.setData(PRESET_ROLE, self.rail._presets.get(value, ""))
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            else:
                tab = self._tabs[value]
                item.setText(tab.info.title)
                item.setData(Qt.ItemDataRole.UserRole, value)
                item.setData(KIND_ROLE, tab.info.tool if tab.info.tool == "shell" else "agent")
                item.setData(GLYPH_ROLE, "square-terminal" if tab.info.tool == "shell" else TOOL_ICONS.get(tab.info.tool, "terminal"))
                item.setData(BADGE_ROLE, self.status_for(tab))
                where = " · ".join(filter(None, (tab.info.account_name, tab.info.environment))) + (" · tmux" if tab.info.tmux and tab.attached else "")
                item.setData(SUB_ROLE, where)
                item.setToolTip(f"{tab.info.project} · {tab.info.title}\n{where}")
            self.rail.addItem(item)
        self.rail.blockSignals(False)
        self._sync_rail_selection()

    def _sync_rail_selection(self) -> None:
        index = self._order.index(self._active) if self._active in self._order else -1
        self.move_up_button.setEnabled(index > 0)
        self.move_down_button.setEnabled(0 <= index < len(self._order) - 1)
        self.rail.blockSignals(True)
        for row in range(self.rail.count()):
            if self.rail.item(row).data(Qt.ItemDataRole.UserRole) == self._active:
                self.rail.setCurrentRow(row)
                break
        self.rail.blockSignals(False)

    def _rail_changed(self, current, previous) -> None:
        tab_id = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        if tab_id and tab_id != self._active:
            self.select(tab_id)

    def _rail_menu(self, pos) -> None:
        item = self.rail.itemAt(pos)
        if item is not None and item.data(KIND_ROLE) == "header":
            self._recall_menu(item.data(PRESET_ROLE) or "", item.text().rsplit(" · ", 1)[0])
            return
        tab_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not tab_id:
            return
        self.select(tab_id)
        tab = self._tabs[tab_id]
        waiting = self.launcher.recoverable.get(tab.info.preset_id, 0)
        actions = [MenuAction(f"召回「{tab.info.project}」的全部工作區（{waiting}）", lambda p=tab.info.preset_id: self.restore_requested.emit(p), "repeat",
                              enabled=waiting > 0, hint="" if waiting else "沒有需要召回的"),
                   MenuAction("同專案改開其他終端…", lambda: self._switch_menu(tab_id, self.rail), "repeat", separator_before=True),
                   MenuAction("離開並保持執行", lambda: self.detach(tab_id), "log-out", enabled=tab.alive() and tab.info.tmux),
                   MenuAction("關閉終端", lambda: self.close_tab(tab_id), "x", danger=True, separator_before=True)]
        ActionMenu(actions, self.window()).popup_below(self.rail.viewport())

    def _recall_menu(self, preset_id: str, project: str) -> None:
        waiting = self.launcher.recoverable.get(preset_id, 0) if preset_id else 0
        action = MenuAction(f"召回「{project}」的全部工作區（{waiting}）", lambda: self.restore_requested.emit(preset_id), "repeat",
                            enabled=waiting > 0, hint="" if waiting else "沒有需要召回的")
        ActionMenu([action], self.window()).popup_below(self.rail.viewport())

    # -- switching agent / environment ----------------------------------------------------------
    def switch_options(self, tab: Tab) -> list[tuple[str, str, str, str, str]]:
        """(label, preset_id, kind, account_id, environment_id) choices for "open something else for this project"."""
        project = next((p for p in self._projects if p.get("id") == tab.info.preset_id), None)
        if project is None:
            return []
        options = []
        env_id = project.get("environment_id", "")      # a project only ever opens on the environment it is bound to
        for tool in ("codex", "claude"):
            account = self.launcher.tool_account(project, tool, env_id)
            if account:
                options.append((f"開啟 {TOOL_LABEL[tool]}（{account.get('name')}）", project["id"], "agent", account["id"], env_id))
        options.append(("開啟 SSH 終端（不執行 Codex／Claude）", project["id"], "shell", "", env_id))
        return options

    def _switch_menu(self, tab_id: str, anchor: QWidget) -> None:
        tab = self._tabs.get(tab_id)
        if tab is None:
            return
        options = self.switch_options(tab)
        if not options:
            self.notify.emit("這個終端不屬於已儲存的專案，無法切換。", "warn")
            return
        actions: list[MenuAction] = []
        for label, preset_id, kind, account_id, environment_id in options:
            actions.append(MenuAction(label, lambda p=preset_id, k=kind, a=account_id, e=environment_id: self.launch_requested.emit(p, k, a, e),
                                      "square-terminal" if kind == "shell" else "terminal"))
        ActionMenu(actions, self.window()).popup_below(anchor)

    def _tool_menu(self, preset_id: str, tool: str, anchor: QWidget) -> None:
        project = next((p for p in self._projects if p.get("id") == preset_id), None)
        if project is None:
            return
        default = self.launcher.tool_account(project, tool)
        actions = []
        for account in self.launcher.fitting(project.get("environment_id", ""), tool):
            kind = "API" if account.get("auth") == "api" else "訂閱"
            hint = f"預設 · {kind}" if default and default.get("id") == account.get("id") else kind
            actions.append(MenuAction(str(account.get("name", "")), lambda a=account["id"]: self.launch_requested.emit(preset_id, "agent", a, ""),
                                      "terminal", hint=hint))
        if actions:
            ActionMenu(actions, self.window()).popup_below(anchor)

    # -- layout / workspaces -----------------------------------------------------------------------
    def set_layout(self, mode: str) -> None:
        if mode not in {"tabs", "grid"}:
            return
        self.layout_bar.set_current(mode, emit=False)
        self.area.set_mode(mode)
        self.layout_changed.emit(mode)

    def slots(self) -> list[dict]:
        """The terminals that can be reopened later (those that belong to a saved project)."""
        out = []
        for tab in self.tabs():
            if tab.info.preset_id and tab.info.kind in {"agent", "shell"}:
                slot = {"preset_id": tab.info.preset_id, "kind": tab.info.kind}
                if tab.info.kind == "agent" and tab.info.account_id:
                    slot["account_id"] = tab.info.account_id
                if tab.info.environment_id:
                    slot["environment_id"] = tab.info.environment_id
                out.append(slot)
        return out

    def _workspace_menu(self) -> None:
        actions = [MenuAction(f"開啟「{w.get('name')}」（{len(w.get('slots', []))} 個終端）", lambda i=w["id"]: self.open_workspace_requested.emit(i), "play")
                   for w in self._workspaces]
        actions.append(MenuAction("將目前的終端儲存為工作區…", self.save_workspace_requested.emit, "save", enabled=bool(self.slots()),
                                  separator_before=bool(actions)))
        for w in self._workspaces:
            actions.append(MenuAction(f"刪除「{w.get('name')}」", lambda i=w["id"]: self.delete_workspace_requested.emit(i), "trash",
                                      danger=True, separator_before=w is self._workspaces[0]))
        ActionMenu(actions, self.window()).popup_below(self.workspace_button)
