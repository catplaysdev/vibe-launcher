"""The 執行環境 page: cards, detail panel, tool + security status, and the page that ties them together."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEasingCurve, QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QFont, QKeyEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from .env_state import (
    AUTH_TEXT, TOOL_LABEL, TOOLS, EnvState, ToolReport, format_local, format_relative, format_tool_report,
    matches_filter, sort_states,
)
from .kit import (
    ActionMenu, Collapsible, ElidedLabel, EmptyState, FilterBar, FlowLayout, Hover, IconButton, IconTile, KeyValueRow,
    MenuAction, PageHeader, SearchField, Section, SplitBody, StatusBadge, draw_focus_ring, glyph_label, lerp_color,
    make_button,
)
from .theme import ACCENT_BORDER_A, BORDER_A, C, M, R, S, T, font, qcolor, section_hue

SECURITY_NOTE = ("密碼期限是本工具的取用政策，不是 OS 隔離；同一 Windows 使用者的其他程式仍可能讀取 Credential Manager。"
                 "OTP 與 passphrase 不保存。")
NO_TOOL_REPORT = "尚未檢查此環境的 CLI 版本。更新後的新工作會重新解析執行檔；既有工作不重啟。"
FILTERS = [("all", "全部"), ("local", "本機"), ("ssh", "SSH"), ("ready", "可用"), ("attention", "需注意")]


def _kind_icon(state: EnvState) -> str:
    return "monitor" if state.kind == "local" else "server"


# --------------------------------------------------------------------------- card


class EnvironmentCard(Hover, QWidget):
    """One environment: identity, status, key chips and a quiet footer. Selection uses an accent edge, not a fill."""

    menu_clicked = Signal(str, object)
    pin_clicked = Signal(str)
    use_clicked = Signal(str)

    def __init__(self, state: EnvState, parent: QWidget | None = None):
        super().__init__(parent)
        self._init_hover()
        self.state = state
        self._selected = False
        self._list_focus = False
        self._sel = 0.0
        self._sel_anim = QVariantAnimation(self)
        self._sel_anim.setDuration(M.hover)
        self._sel_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._sel_anim.valueChanged.connect(self._set_sel)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAutoFillBackground(False)

        root = QVBoxLayout(self)
        root.setContentsMargins(S.lg + 2, S.md + 2, S.md, S.md)
        root.setSpacing(S.md)

        head = QHBoxLayout()
        head.setSpacing(S.md)
        self.tile = IconTile(40, 20)
        head.addWidget(self.tile, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(1)
        self.name = ElidedLabel("", px=T.section, weight=QFont.Weight.DemiBold)
        self.sub = ElidedLabel("", px=T.meta + 1, color="text_secondary")
        text.addWidget(self.name)
        text.addWidget(self.sub)
        head.addLayout(text, 1)
        self.badge = StatusBadge()
        head.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        self.pin = IconButton("star", "釘選到最上方", size=28, icon_size=15)
        self.pin.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        sp = self.pin.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self.pin.setSizePolicy(sp)
        self.pin.clicked.connect(lambda: self.pin_clicked.emit(self.state.id))
        head.addWidget(self.pin, 0, Qt.AlignmentFlag.AlignTop)
        self.more = IconButton("more", "更多動作", size=28, icon_size=16)
        self.more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.more.clicked.connect(lambda: self.menu_clicked.emit(self.state.id, self.more))
        head.addWidget(self.more, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)

        self.chip_host = QWidget()
        self.chips = FlowLayout(self.chip_host)
        self.chips.setContentsMargins(0, 0, 0, 0)
        self._chip_key: tuple | None = None
        root.addWidget(self.chip_host)

        foot = QHBoxLayout()
        foot.setSpacing(S.sm)
        self.recent = ElidedLabel("", px=T.meta, color="text_muted")
        foot.addWidget(self.recent, 1)
        self.use = make_button("使用此環境", "ghost", "arrow-right", small=True)
        self.use.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.use.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.use.clicked.connect(lambda: self.use_clicked.emit(self.state.id))
        foot.addWidget(self.use)
        root.addLayout(foot)
        self.set_state(state)

    # -- state ---------------------------------------------------------------
    def set_state(self, state: EnvState, checking: bool = False) -> None:
        self.state = state
        if state.running_sessions:   # running environments are distinguishable at a glance
            self.tile.set_icon(_kind_icon(state), "success", ("success", 0.14))
        else:
            self.tile.set_icon(_kind_icon(state), "text_secondary", ("border", 0.12))
        self.name.setText(state.name)
        self.sub.setText(state.subtitle)
        if checking:
            self.badge.set_state("檢查中", "unknown", "", "正在等待終端回報檢查結果", spinning=True)
        else:
            self.badge.set_state(state.status_text, state.tone, "dot", state.status_tip)
        self._sync_pin()
        key = tuple(state.chips)
        if key != self._chip_key:
            self._chip_key = key
            self.chips.clear()
            for chip in state.chips:
                self.chips.addWidget(StatusBadge(chip.label, chip.tone, chip.icon, chip.tip, max_width=200))
            self.chip_host.updateGeometry()
        self.refresh_time()
        self.setAccessibleName(f"{state.name}，{'本機' if state.kind == 'local' else 'SSH'}，狀態：{state.status_text}")
        self.setAccessibleDescription("；".join(c.label for c in state.chips))
        self.update()

    def refresh_time(self) -> None:
        self.recent.setText("最近使用 " + format_relative(self.state.last_used) if self.state.last_used else "尚未使用")

    def _sync_pin(self) -> None:
        pinned = self.state.pinned
        self.pin.active_color = "warning" if pinned else None
        self.pin.setToolTip("取消釘選" if pinned else "釘選到最上方")
        self.pin.setAccessibleName(self.pin.toolTip())
        self.pin.setVisible(pinned or self._hv > 0.05 or self._selected)

    # -- selection / hover -------------------------------------------------------
    def set_selected(self, selected: bool, ring: bool) -> None:
        """``ring`` = show the strong keyboard-focus outline (not for plain mouse selection)."""
        self._list_focus = ring
        if selected != self._selected:
            self._selected = selected
            self._sel_anim.stop()
            self._sel_anim.setStartValue(self._sel)
            self._sel_anim.setEndValue(1.0 if selected else 0.0)
            self._sel_anim.start()
        self._sync_pin()
        self.update()

    def _set_sel(self, value) -> None:
        self._sel = float(value)
        self.update()

    def _set_hover(self, value) -> None:
        super()._set_hover(value)
        self._sync_pin()

    def height_for_width(self, width: int) -> int:
        return self.layout().totalHeightForWidth(width)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        base = lerp_color(C.surface, C.surface_hover, self._hv)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(base)
        p.drawRoundedRect(rect, R.xl, R.xl)
        if self._sel > 0:
            p.setBrush(qcolor(C.accent, 0.07 * self._sel))
            p.drawRoundedRect(rect, R.xl, R.xl)
        border_alpha = BORDER_A + 0.10 * self._hv
        border = qcolor(C.border, border_alpha)
        width = 1.0
        if self._sel > 0:
            focused = self._list_focus
            accent_alpha = (0.85 if focused else ACCENT_BORDER_A) * self._sel
            border = lerp_color(C.border, C.accent, self._sel, border_alpha + (accent_alpha - border_alpha) * self._sel)
            width = 1.0 + (1.0 if focused else 0.0) * self._sel
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(border, width))
        p.drawRoundedRect(rect.adjusted(width / 2 - 0.5, width / 2 - 0.5, -(width / 2 - 0.5), -(width / 2 - 0.5)), R.xl, R.xl)
        if self._sel > 0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(C.accent, self._sel))
            p.drawRoundedRect(QRectF(1.5, 16, 3, max(8, rect.height() - 32)), 1.5, 1.5)


class CardList(QListWidget):
    """QListWidget (keeps arrow/Home/End/type-ahead navigation) rendering cards as item widgets."""

    focus_changed = Signal(bool)
    delete_pressed = Signal()
    menu_pressed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.keyboard_ring = False
        self.setProperty("cards", True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSpacing(S.xs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName("環境清單")
        self.verticalScrollBar().setSingleStep(24)

    def _set_ring(self, value: bool) -> None:
        if value != self.keyboard_ring:
            self.keyboard_ring = value
            self.focus_changed.emit(value)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self._set_ring(event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                                          Qt.FocusReason.ShortcutFocusReason))
        self.focus_changed.emit(self.keyboard_ring)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.keyboard_ring = False
        self.focus_changed.emit(False)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._set_ring(False)
        super().mousePressEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.relayout_items()

    def relayout_items(self) -> None:
        width = max(0, self.viewport().width() - 2 * self.spacing())
        for row in range(self.count()):
            item = self.item(row)
            widget = self.itemWidget(item)
            if isinstance(widget, EnvironmentCard):
                item.setSizeHint(QSize(width, widget.height_for_width(width)))
            elif widget is not None:
                item.setSizeHint(QSize(width, widget.sizeHint().height()))

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Home, Qt.Key.Key_End, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            self._set_ring(True)
        if event.key() == Qt.Key.Key_Delete:
            self.delete_pressed.emit()
            return
        if event.key() == Qt.Key.Key_Menu or (event.key() == Qt.Key.Key_F10 and event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.menu_pressed.emit()
            return
        super().keyPressEvent(event)


class _GroupHeader(QWidget):
    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(S.xs, S.md, 0, 0)
        self.label = ElidedLabel(text, px=T.micro, weight=QFont.Weight.DemiBold, color="text_muted")
        row.addWidget(self.label)
        self.setFixedHeight(28)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(100, 28)


# --------------------------------------------------------------------------- tool + security


class ToolStatusRow(QWidget):
    """``Codex  Installed 0.x → Latest 0.y   [status]  [Update]`` — Update only appears when it can help."""

    update_clicked = Signal(str)

    def __init__(self, tool: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.tool = tool
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(S.md)
        self.tile = IconTile(36, 18, radius=R.md)
        self.tile.set_icon("terminal", "text_secondary", ("border", 0.12))
        row.addWidget(self.tile)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.name = ElidedLabel(TOOL_LABEL[tool], px=T.body + 1, weight=QFont.Weight.DemiBold)
        self.meta = ElidedLabel("", px=T.meta, color="text_muted")
        col.addWidget(self.name)
        col.addWidget(self.meta)
        row.addLayout(col, 1)
        self.badge = StatusBadge()
        row.addWidget(self.badge)
        self.update_button = make_button("更新", "subtle", "arrow-up-circle", small=True)
        self.update_button.clicked.connect(lambda: self.update_clicked.emit(self.tool))
        row.addWidget(self.update_button)
        self.set_report(None)

    def set_report(self, report: ToolReport | None, busy: str = "") -> None:
        label = TOOL_LABEL[self.tool]
        show_update = False
        if busy:
            self.badge.set_state("更新中" if busy == "update" else "檢查中", "unknown", "", "正在等待終端回報結果", spinning=True)
            self.meta.setText("已在終端開啟，完成後會自動更新這裡")
        elif report is None:
            self.badge.set_state("尚未檢查", "unknown", "help", f"尚未檢查 {label} 版本")
            self.meta.setText("按「檢查更新」確認已安裝與最新版本")
        else:
            parts = []
            if report.installed != "unknown":
                parts.append(f"已安裝 {report.installed}")
            if report.latest != "unknown":
                parts.append(f"最新 {report.latest}")
            if report.source and report.source != "unknown":
                parts.append(report.source)
            self.meta.setText(" · ".join(parts) or (report.message or "未偵測到"))
            tip = report.message or ""
            if report.status == "update_failed":
                self.badge.set_state("更新失敗", "danger", "x-circle", tip)
                show_update = True
            elif report.installed == "unknown":
                self.badge.set_state("未安裝", "unknown", "circle-dashed", tip or f"此環境找不到 {label} CLI")
            elif report.status == "update_available":
                self.badge.set_state("有新版", "info", "arrow-up-circle", f"{report.installed} → {report.latest}")
                show_update = True
            elif report.status == "newer_than_registry":
                self.badge.set_state("版本較新", "ok", "check", "比官方 registry 更新")
            else:  # current, or no update detected: shown as up to date
                self.badge.set_state("已是最新", "ok", "check",
                                     "已是最新版本" if report.status == "current" else "未偵測到新版本")
        self.update_button.setVisible(show_update)
        self.update_button.setText("重試" if report is not None and report.status == "update_failed" else "更新")
        self.update_button.setAccessibleName(f"更新 {label}")


class SecurityStatus(QWidget):
    """Credential status as a state, not a log: one headline, one action, one honest footnote."""

    lock_clicked = Signal()
    adjust_clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(S.md)
        row = QHBoxLayout()
        row.setSpacing(S.md + 2)
        self.tile = IconTile(40, 20)
        row.addWidget(self.tile, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = QLabel()
        self.title.setStyleSheet(f"font-size: {T.body + 1}px; font-weight: 600;")
        self.title.setWordWrap(True)
        self.title.setMinimumWidth(0)
        self.subtitle = QLabel()
        self.subtitle.setProperty("role", "muted")
        self.subtitle.setWordWrap(True)
        self.subtitle.setMinimumWidth(0)
        col.addWidget(self.title)
        col.addWidget(self.subtitle)
        row.addLayout(col, 1)
        self.lock_button = make_button("立即鎖定", "subtle", "lock", small=True,
                                       tip="立即清除已保存的 SSH 密碼；下次連線需重新輸入")
        self.lock_button.clicked.connect(self.lock_clicked)
        row.addWidget(self.lock_button, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(row)
        self.adjust_button = make_button("調整保存期限…", "ghost", "pencil", small=True,
                                         tip="編輯此環境，變更或關閉密碼保存期限")
        self.adjust_button.clicked.connect(self.adjust_clicked)
        root.addWidget(self.adjust_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.note_row = QWidget()
        note = QHBoxLayout(self.note_row)
        note.setContentsMargins(0, 0, 0, 0)
        note.setSpacing(S.sm)
        glyph = glyph_label("info", "text_muted", 14)
        glyph.setFixedWidth(16)
        note.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)
        self.note = QLabel(SECURITY_NOTE)
        self.note.setProperty("role", "meta")
        self.note.setWordWrap(True)
        self.note.setMinimumWidth(0)
        note.addWidget(self.note, 1)
        root.addWidget(self.note_row)

    def set_state(self, state: EnvState | None) -> None:
        mode = state.credential if state else "local"
        title, sub, icon, color, lock = {
            "local": ("本機環境", "不使用 SSH 憑證；Codex 與 Claude 使用各自的官方登入。", "shield", "text_secondary", False),
            "interactive": ("互動驗證", "密碼與 OTP 在終端輸入，從不保存。", "key", "text_secondary", False),
            "key": ("SSH key／agent", "Passphrase 不會保存，由 ssh-agent 或終端處理。", "key", "text_secondary", False),
            "password_off": ("密碼不保存", "每次連線手動輸入，不會寫入 Credential Manager。", "unlock", "text_secondary", False),
            "password_cached": ("由 Windows Credential Manager 保護", "", "shield-check", "success", True),
            "password_persistent": ("密碼持續保存", "不會自動到期，會一直保存在 Windows Credential Manager，直到你鎖定。可改成限時保存。", "lock", "warning", True),
        }[mode]
        if mode == "password_cached" and state:
            from .askpass import POLICIES
            window = {seconds: text for text, seconds in POLICIES}.get(state.remember_seconds, "")
            sub = f"密碼最多保存 {window}，連線成功後才會保存。"
        self.tile.set_icon(icon, color)
        self.title.setText(title)
        self.subtitle.setText(sub)
        self.lock_button.setVisible(lock)
        self.adjust_button.setVisible(lock and not (state and state.builtin))
        # The Credential Manager caveat only matters when a password can actually be saved.
        self.note_row.setVisible(lock)


# --------------------------------------------------------------------------- detail panel


class EnvironmentDetail(QStackedWidget):
    """Right-hand panel. Frequent info is always visible; the rest folds into 安全性詳細 / 診斷資訊."""

    primary_clicked = Signal()
    edit_clicked = Signal()
    check_clicked = Signal()
    ssh_settings_clicked = Signal()
    pin_clicked = Signal()
    menu_clicked = Signal(object)
    check_tools_clicked = Signal()
    update_tool_clicked = Signal(str)
    lock_clicked = Signal()
    close_clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.state: EnvState | None = None
        self._busy: set[str] = set()

        self.placeholder = EmptyState()
        self.placeholder.set_content("layers", "選取一個環境", "從左側清單選擇環境，即可查看連線、工具與憑證狀態。")
        self.addWidget(self.placeholder)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        scroll.setWidget(content)
        self.scroll = scroll
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, S.xs, S.lg)
        root.setSpacing(S.lg)
        self.addWidget(scroll)

        # header --------------------------------------------------------------
        head = QHBoxLayout()
        head.setSpacing(S.md + 2)
        self.tile = IconTile(48, 24, radius=R.lg + 2)
        head.addWidget(self.tile, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.name = ElidedLabel("", px=T.detail_title, weight=QFont.Weight.DemiBold)
        self.sub = ElidedLabel("", px=T.body, color="text_secondary")
        text.addWidget(self.name)
        text.addWidget(self.sub)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, S.xs, 0, 0)
        self.badge = StatusBadge()
        status_row.addWidget(self.badge)
        status_row.addStretch(1)
        text.addLayout(status_row)
        head.addLayout(text, 1)
        self.pin = IconButton("star", "釘選到最上方", size=36, icon_size=18)
        self.pin.clicked.connect(self.pin_clicked)
        head.addWidget(self.pin, 0, Qt.AlignmentFlag.AlignTop)
        self.close_button = IconButton("x", "關閉詳細資訊", size=36, icon_size=18)
        self.close_button.clicked.connect(self.close_clicked)
        self.close_button.hide()
        head.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)

        # actions: one primary, a few quiet secondaries, everything risky behind the overflow ------------
        self.actions_host = QWidget()
        actions = FlowLayout(self.actions_host, S.sm, S.sm)
        actions.setContentsMargins(0, 0, 0, 0)
        self.primary = make_button("使用此環境", "primary", "arrow-right",
                                   tip="以此環境建立專案（Enter）")
        self.primary.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.primary.clicked.connect(self.primary_clicked)
        self.check = make_button("環境檢查", "secondary", "check-circle", tip="在終端檢查連線、CLI 與 Agent")
        self.check.clicked.connect(self.check_clicked)
        self.ssh_settings = make_button("SSH 設定", "secondary", "sliders", tip="解析既有 SSH config（不連線、不讀私鑰）")
        self.ssh_settings.clicked.connect(self.ssh_settings_clicked)
        self.edit = make_button("編輯", "secondary", "pencil")
        self.edit.clicked.connect(self.edit_clicked)
        self.more = IconButton("more", "更多動作", size=36, icon_size=18)
        self.more.clicked.connect(lambda: self.menu_clicked.emit(self.more))
        for widget in (self.primary, self.check, self.ssh_settings, self.edit, self.more):
            actions.addWidget(widget)
        root.addWidget(self.actions_host)

        # connection -----------------------------------------------------------------
        self.connection = Section("連線")
        self.row_kind = KeyValueRow("類型", "")
        self.row_host = KeyValueRow("主機", "", mono=True, copyable=True)
        self.row_identity = KeyValueRow("身分檔", "", mono=True)
        self.row_proxy = KeyValueRow("ProxyJump", "", mono=True)
        self.row_auth = KeyValueRow("驗證方式", "")
        self.row_last = KeyValueRow("最近使用", "")
        for row in (self.row_kind, self.row_host, self.row_identity, self.row_proxy, self.row_auth, self.row_last):
            self.connection.body.addWidget(row)
        root.addWidget(self.connection)

        # developer tools ------------------------------------------------------------
        self.tools = Section("開發工具")
        self.check_tools = make_button("檢查更新", "ghost", "refresh", small=True, tip="檢查 Codex 與 Claude 是否有新版")
        self.check_tools.clicked.connect(self.check_tools_clicked)
        self.tools.add_head_widget(self.check_tools)
        self.tools_more = IconButton("more", "更新 CLI…", size=28, icon_size=16)
        self.tools_more.clicked.connect(lambda: self.menu_clicked.emit(("tools", self.tools_more)))
        self.tools.add_head_widget(self.tools_more)
        self.tool_rows = {tool: ToolStatusRow(tool) for tool in TOOLS}
        for row in self.tool_rows.values():
            row.update_clicked.connect(self.update_tool_clicked)
            self.tools.body.addWidget(row)
        root.addWidget(self.tools)

        # credential / security ----------------------------------------------------
        self.security = Section("憑證與安全")
        self.security_status = SecurityStatus()
        self.security_status.lock_clicked.connect(self.lock_clicked)
        self.security_status.adjust_clicked.connect(self.edit_clicked)
        self.security.body.addWidget(self.security_status)
        root.addWidget(self.security)

        self.security_details = Collapsible("安全性詳細")
        self.row_trust = KeyValueRow("主機信任", "")
        self.row_fingerprint = KeyValueRow("主機指紋", "", mono=True, copyable=True)
        self.row_openssh = KeyValueRow("OpenSSH", "")
        self.row_agent = KeyValueRow("SSH Agent", "")
        for row in (self.row_trust, self.row_fingerprint, self.row_openssh, self.row_agent):
            self.security_details.body.addWidget(row)
        root.addWidget(self.security_details)

        self.diagnostics = Collapsible("診斷資訊")
        self.tool_versions_label = QLabel(NO_TOOL_REPORT)
        self.tool_versions_label.setTextFormat(Qt.TextFormat.PlainText)
        self.tool_versions_label.setWordWrap(True)
        self.tool_versions_label.setMinimumWidth(0)
        self.tool_versions_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.tool_versions_label.setFont(font(T.meta, mono=True))
        self.tool_versions_label.setProperty("role", "muted")
        self.diagnostics.body.addWidget(self.tool_versions_label)
        root.addWidget(self.diagnostics)
        root.addStretch(1)
        self.set_state(None)

    @property
    def security_status_label(self) -> QLabel:
        return self.security_status.note

    def set_drawer_mode(self, drawer: bool) -> None:
        self.close_button.setVisible(drawer)

    def set_busy(self, kinds: set[str]) -> None:
        self._busy = set(kinds)
        if self.state is not None:
            self._apply_busy()

    # -- rendering -----------------------------------------------------------------
    def set_state(self, state: EnvState | None) -> None:
        self.state = state
        if state is None:
            self.setCurrentIndex(0)
            self.security_status.set_state(None)
            self.tool_versions_label.setText("選取環境後檢查 CLI 版本。")
            return
        self.setCurrentIndex(1)
        ssh = state.kind == "ssh"
        running_color = "success" if state.running_sessions else "accent"
        self.tile.set_icon(_kind_icon(state), running_color, (running_color, 0.10))
        self.name.setText(state.name)
        self.sub.setText(state.subtitle)
        self.pin.active_color = "warning" if state.pinned else None
        self.pin.setToolTip("取消釘選" if state.pinned else "釘選到最上方")
        self.pin.setAccessibleName(self.pin.toolTip())
        self.ssh_settings.setVisible(ssh)
        self.more.setAccessibleName(f"{state.name} 的更多動作")

        self.row_kind.set_value("SSH · 遠端" if ssh else "本機 · Local")
        if ssh:
            self.row_host.key_label.setText("主機")
            self.row_host.set_value(state.target)
            self.row_identity.set_value(list(state.identity) or ["尚未解析 · 按「SSH 設定」讀取既有 config"],
                                        "text" if state.identity else "text_muted", None if state.identity else False)
            self.row_proxy.set_value(("無" if state.proxyjump.lower() == "none" else state.proxyjump) if state.proxyjump else "尚未檢查",
                                     "text" if state.proxyjump else "text_muted",
                                     None if state.proxyjump and state.proxyjump.lower() != "none" else False)
            auth = {"interactive": "互動驗證（終端輸入）", "key": "SSH key／agent",
                    "password": "密碼 · Windows Credential Manager" if state.remember_seconds else "密碼 · 每次輸入"}
            self.row_auth.set_value(auth.get(state.auth_mode, AUTH_TEXT.get(state.auth_mode, state.auth_mode)))
        else:
            self.row_host.set_value("localhost")
            self.row_auth.set_value("使用各工具的官方登入")
        for row in (self.row_identity, self.row_proxy):
            row.setVisible(ssh)
        self.row_last.set_value(format_relative(state.last_used) if state.last_used else "尚未使用",
                                "text" if state.last_used else "text_muted")

        for tool, row in self.tool_rows.items():
            row.set_report(state.tool(tool))
        self.security_status.set_state(state)
        self.security_details.setVisible(ssh)
        if ssh:
            if state.trust_ok:
                self.row_trust.set_value("已保存主機金鑰；連線時仍會驗證", "success")
            else:
                self.row_trust.set_value("尚未核對主機指紋（首次連線時確認）", "text_secondary")
            self.row_fingerprint.set_value(state.fingerprint.splitlines() or ["尚無已核對指紋"],
                                           "text" if state.fingerprint else "text_muted", None if state.fingerprint else False)
            self.row_openssh.set_value("未安裝 Windows OpenSSH Client" if state.openssh_missing else (state.ssh_version or "尚未檢查"),
                                       "danger" if state.openssh_missing else ("text" if state.ssh_version else "text_muted"))
            agent = (state.agent + (" · " + format_local(state.diag_checked_at) if state.diag_checked_at else "")) if state.agent_checked \
                else "尚未檢查 · 使用環境檢查確認（不讀取私鑰）"
            self.row_agent.set_value(agent, "text" if state.agent_checked else "text_muted")
        self.tool_versions_label.setText("\n\n".join(format_tool_report(r) for r in state.tools) or NO_TOOL_REPORT)
        self._apply_busy()

    def _apply_busy(self) -> None:
        state = self.state
        if state is None:
            return
        env_busy = bool(self._busy & {"doctor", "ssh"})
        tools_busy = "tools" in self._busy
        if env_busy:
            self.badge.set_state("檢查中", "unknown", "", "正在等待終端回報檢查結果", spinning=True)
        else:
            self.badge.set_state(state.status_text, state.tone, "dot", state.status_tip)
        self.check.setEnabled(not env_busy)
        self.check.setText("檢查中…" if env_busy else "環境檢查")
        self.ssh_settings.setEnabled("ssh" not in self._busy)
        self.check_tools.setEnabled(not tools_busy)
        self.check_tools.setText("檢查中…" if tools_busy else "檢查更新")
        for tool, row in self.tool_rows.items():
            if f"update:{tool}" in self._busy:
                row.set_report(state.tool(tool), busy="update")
            elif tools_busy:
                row.set_report(state.tool(tool), busy="check")
            else:
                row.set_report(state.tool(tool))


# --------------------------------------------------------------------------- page


class EnvironmentsPage(QWidget):
    """Header + filters + card list + detail. Emits intent signals; the window owns all behaviour."""

    add_requested = Signal()
    palette_requested = Signal()
    primary_requested = Signal(str)
    edit_requested = Signal(str)
    check_requested = Signal(str)
    ssh_settings_requested = Signal(str)
    delete_requested = Signal(str)
    forget_requested = Signal(str)
    lock_requested = Signal(str)
    pin_toggled = Signal(str)
    check_tools_requested = Signal(str)
    update_tool_requested = Signal(str, str)
    selection_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._states: dict[str, EnvState] = {}
        self._cards: dict[str, EnvironmentCard] = {}
        self._rows_key: list[tuple] = []
        self._selected_id: str | None = None
        self._busy: dict[str, set[str]] = {}
        self._menu_depth = 0
        self._pending: tuple[list[EnvState], dict[str, set[str]]] | None = None
        self._open_menu: ActionMenu | None = None

        self.root = QVBoxLayout(self)
        self.root.setSpacing(S.lg)

        self.header = PageHeader("執行環境", "管理 Local / SSH 工作環境與登入狀態", icon="layers", hue=section_hue("environments"))
        self.search = SearchField("搜尋環境、主機")
        self.search.setMinimumWidth(160)
        self.search.setMaximumWidth(300)
        self.search.setToolTip("搜尋環境（Ctrl+F）")
        self.search.textChanged.connect(lambda _: self._rerender())
        self.palette_button = make_button("Ctrl K", "ghost", "command", tip="指令面板（Ctrl+K）")
        self.palette_button.clicked.connect(self.palette_requested)
        self.add_button = make_button("新增環境", "primary", "plus", tip="新增 Local 或 SSH 環境（Ctrl+N）")
        self.add_button.clicked.connect(self.add_requested)
        self.header.add_action(self.search, 1)
        self.header.add_action(self.palette_button)
        self.header.add_action(self.add_button)
        self.root.addWidget(self.header)

        # list column --------------------------------------------------------------
        self.list_column = QWidget()
        column = QVBoxLayout(self.list_column)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(S.md)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(S.md)
        self.filters = FilterBar(FILTERS)
        self.filters.changed.connect(lambda _: self._rerender())
        filter_row.addWidget(self.filters, 1)
        column.addLayout(filter_row)
        self.environments_list = CardList()
        self.environments_list.currentItemChanged.connect(self._current_changed)
        self.environments_list.itemClicked.connect(self._item_clicked)
        self.environments_list.itemActivated.connect(self._item_activated)
        self.environments_list.focus_changed.connect(lambda _: self._sync_selection())
        self.environments_list.delete_pressed.connect(self._delete_current)
        self.environments_list.menu_pressed.connect(self._menu_current)
        self.no_match = EmptyState()
        self.no_match.set_content("search", "沒有符合的環境", "換個關鍵字，或清除篩選條件。", secondary="清除搜尋與篩選")
        self.no_match.secondary.clicked.connect(self.clear_filters)
        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self.environments_list)
        self.list_stack.addWidget(self.no_match)
        column.addWidget(self.list_stack, 1)

        self.detail = EnvironmentDetail()
        self.detail.primary_clicked.connect(lambda: self._emit_for_selected(self.primary_requested))
        self.detail.edit_clicked.connect(lambda: self._emit_for_selected(self.edit_requested))
        self.detail.check_clicked.connect(lambda: self._emit_for_selected(self.check_requested))
        self.detail.ssh_settings_clicked.connect(lambda: self._emit_for_selected(self.ssh_settings_requested))
        self.detail.pin_clicked.connect(lambda: self._emit_for_selected(self.pin_toggled))
        self.detail.lock_clicked.connect(lambda: self._emit_for_selected(self.lock_requested))
        self.detail.check_tools_clicked.connect(lambda: self._emit_for_selected(self.check_tools_requested))
        self.detail.update_tool_clicked.connect(self._update_tool)
        self.detail.menu_clicked.connect(self._detail_menu)
        self.detail.close_clicked.connect(lambda: self.body.close_drawer())

        self.body = SplitBody(self.list_column, self.detail)
        self.page_stack = QStackedWidget()
        self.page_stack.addWidget(self.body)
        self.empty = EmptyState()
        self.empty.set_content("layers", "尚未建立執行環境",
                               "新增 Local 或 SSH Environment，讓 Vibe Launcher 幫你管理 CLI、Credential 與工作階段。",
                               primary="新增第一個環境")
        self.empty.primary.clicked.connect(self.add_requested)
        self.page_stack.addWidget(self.empty)
        self.root.addWidget(self.page_stack, 1)
        self.set_compact(False)

    # -- responsive ------------------------------------------------------------------
    def set_compact(self, compact: bool) -> None:
        m = S.lg if compact else S.xl
        self.root.setContentsMargins(m, m, m, m)

    def set_layout_mode(self, mode: str) -> None:
        self.body.set_mode(mode)
        self.detail.set_drawer_mode(mode == "drawer")

    # -- data ---------------------------------------------------------------------------
    def set_states(self, states: list[EnvState], busy: dict[str, set[str]] | None = None) -> None:
        if self._menu_depth:
            self._pending = (states, busy or {})
            return
        self._states = {s.id: s for s in states}
        self._busy = busy or {}
        self._rerender()

    def state_for(self, env_id: str | None) -> EnvState | None:
        return self._states.get(env_id or "")

    def selected_id(self) -> str | None:
        item = self.environments_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def select(self, env_id: str) -> bool:
        self._selected_id = env_id
        if env_id not in self._states:
            return False
        state = self._states[env_id]
        if not self._passes(state):
            self.search.clear()
            self.filters.set_current("all")
        for row in range(self.environments_list.count()):
            item = self.environments_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == env_id:
                self.environments_list.setCurrentItem(item)
                return True
        return False

    def focus_search(self) -> None:
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def clear_filters(self) -> None:
        self.search.clear()
        self.filters.set_current("all")
        self._rerender()

    def refresh_times(self) -> None:
        for card in self._cards.values():
            card.refresh_time()

    # -- filtering / rendering -----------------------------------------------------------
    def _term_matches(self, state: EnvState) -> bool:
        term = self.search.text().strip().casefold()
        if not term:
            return True
        hay = " ".join([state.name, state.target, state.subtitle, state.status_text, state.kind,
                        *(c.label for c in state.chips)]).casefold()
        return all(t in hay for t in term.split())

    def _passes(self, state: EnvState) -> bool:
        return self._term_matches(state) and matches_filter(state, self.filters.current())

    def _rerender(self) -> None:
        states = list(self._states.values())
        matched = [s for s in states if self._term_matches(s)]
        self.filters.set_counts({key: sum(1 for s in matched if matches_filter(s, key)) for key, _ in FILTERS})
        if not states:
            self.page_stack.setCurrentWidget(self.empty)
            self.environments_list.clear()
            self._cards.clear()
            self._rows_key = []
            self.detail.set_state(None)
            return
        self.page_stack.setCurrentWidget(self.body)
        visible = sort_states([s for s in matched if matches_filter(s, self.filters.current())])
        rows: list[tuple] = []
        if any(s.pinned for s in visible) and any(not s.pinned for s in visible):
            pinned = [s for s in visible if s.pinned]
            rest = [s for s in visible if not s.pinned]
            rows = [("header", "已釘選"), *[("env", s.id) for s in pinned], ("header", "其他環境"), *[("env", s.id) for s in rest]]
        else:
            rows = [("env", s.id) for s in visible]
        self.list_stack.setCurrentWidget(self.environments_list if visible else self.no_match)

        if rows != self._rows_key:
            self._rebuild(rows)
        for env_id, card in self._cards.items():
            card.set_state(self._states[env_id], checking=bool(self._busy.get(env_id, set()) & {"doctor", "ssh"}))
        self.environments_list.relayout_items()
        self._ensure_selection()
        self._sync_selection()

    def _rebuild(self, rows: list[tuple]) -> None:
        lst = self.environments_list
        lst.blockSignals(True)
        lst.clear()
        self._cards.clear()
        for kind, value in rows:
            item = QListWidgetItem()
            lst.addItem(item)
            if kind == "header":
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                lst.setItemWidget(item, _GroupHeader(value))
                continue
            state = self._states[value]
            item.setData(Qt.ItemDataRole.UserRole, value)
            card = EnvironmentCard(state)
            card.menu_clicked.connect(self._card_menu)
            card.pin_clicked.connect(lambda i: self._emit_for(i, self.pin_toggled))
            card.use_clicked.connect(lambda i: self._emit_for(i, self.primary_requested))
            lst.setItemWidget(item, card)
            self._cards[value] = card
        self._rows_key = rows
        lst.blockSignals(False)

    def _ensure_selection(self) -> None:
        lst = self.environments_list
        target = None
        for row in range(lst.count()):
            item = lst.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == self._selected_id:
                target = item
                break
        if target is None:
            for row in range(lst.count()):
                item = lst.item(row)
                if item.flags() & Qt.ItemFlag.ItemIsEnabled and item.data(Qt.ItemDataRole.UserRole):
                    target = item
                    break
        lst.blockSignals(True)
        if target is not None:
            lst.setCurrentItem(target)
        else:
            lst.setCurrentRow(-1)
        lst.blockSignals(False)

    def _sync_selection(self) -> None:
        current = self.selected_id()
        ring = self.environments_list.hasFocus() and self.environments_list.keyboard_ring
        for env_id, card in self._cards.items():
            card.set_selected(env_id == current, ring)
        changed = current != self._selected_id
        self._selected_id = current if current else self._selected_id
        state = self._states.get(current or "")
        if state is None:
            self.detail.set_state(None)
        else:
            if self.detail.state != state:
                self.detail.set_state(state)
            self.detail.set_busy(self._busy.get(state.id, set()))
        if changed and current:
            self.selection_changed.emit(current)

    def _current_changed(self, current, previous) -> None:
        self._sync_selection()

    # -- interactions ------------------------------------------------------------------------
    def _item_clicked(self, item) -> None:
        if self.body.mode() == "drawer" and item.data(Qt.ItemDataRole.UserRole):
            self.body.open_drawer()

    def _item_activated(self, item) -> None:
        env_id = item.data(Qt.ItemDataRole.UserRole)
        if not env_id:
            return
        if self.body.mode() == "drawer":
            self.body.open_drawer()
        else:
            self.primary_requested.emit(env_id)

    def _emit_for_selected(self, signal) -> None:
        env_id = self.selected_id()
        if env_id:
            signal.emit(env_id)

    def _emit_for(self, env_id: str, signal) -> None:
        self.select(env_id)
        signal.emit(env_id)

    def _update_tool(self, tool: str) -> None:
        env_id = self.selected_id()
        if env_id:
            self.update_tool_requested.emit(env_id, tool)

    def _delete_current(self) -> None:
        self._emit_for_selected(self.delete_requested)

    def _menu_current(self) -> None:
        env_id = self.selected_id()
        card = self._cards.get(env_id or "")
        if card is not None:
            self._card_menu(env_id, card.more)

    def _card_menu(self, env_id: str, anchor: QWidget) -> None:
        self.select(env_id)
        self._show_menu(self._env_actions(self._states[env_id]), anchor)

    def _detail_menu(self, payload) -> None:
        if isinstance(payload, tuple):
            _, anchor = payload
            self._show_menu([MenuAction("更新 Codex…", lambda: self._update_tool("codex"), "arrow-up-circle"),
                             MenuAction("更新 Claude…", lambda: self._update_tool("claude"), "arrow-up-circle")], anchor)
            return
        state = self._states.get(self.selected_id() or "")
        if state is not None:
            self._show_menu(self._env_actions(state, include_primary=False), payload)

    def _env_actions(self, state: EnvState, include_primary: bool = True) -> list[MenuAction]:
        ssh = state.kind == "ssh"
        cached = state.credential in {"password_cached", "password_persistent"}
        actions: list[MenuAction] = []
        if include_primary:
            actions += [MenuAction("使用此環境", lambda: self.primary_requested.emit(state.id), "arrow-right"),
                        MenuAction("環境檢查", lambda: self.check_requested.emit(state.id), "check-circle"),
                        MenuAction("編輯", lambda: self.edit_requested.emit(state.id), "pencil")]
            if ssh:
                actions.insert(2, MenuAction("SSH 設定", lambda: self.ssh_settings_requested.emit(state.id), "sliders"))
        actions.append(MenuAction("取消釘選" if state.pinned else "釘選到最上方", lambda: self.pin_toggled.emit(state.id), "star",
                                  separator_before=include_primary))
        if ssh:
            # "Lock" and "forget" are the same operation (revoke the cached password), so offer it once.
            actions.append(MenuAction("鎖定 Credential（清除已存密碼）", lambda: self.lock_requested.emit(state.id), "lock",
                                      separator_before=True))
        actions.append(MenuAction("刪除環境" if not state.builtin else "刪除環境（內建，不可刪除）",
                                  lambda: self.delete_requested.emit(state.id), "trash", danger=True,
                                  enabled=not state.builtin, separator_before=True))
        return actions

    def _show_menu(self, actions: list[MenuAction], anchor: QWidget) -> None:
        menu = ActionMenu(actions, self.window())
        menu.about_to_show.connect(self._menu_shown)
        menu.about_to_hide.connect(self._menu_hidden)
        self._open_menu = menu
        menu.popup_below(anchor)

    def _menu_shown(self) -> None:
        self._menu_depth += 1

    def _menu_hidden(self) -> None:
        self._menu_depth = max(0, self._menu_depth - 1)
        if not self._menu_depth and self._pending is not None:
            states, busy = self._pending
            self._pending = None
            self.set_states(states, busy)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self.body.drawer_open():
            self.body.close_drawer()
            self.environments_list.setFocus()
            return
        super().keyPressEvent(event)
