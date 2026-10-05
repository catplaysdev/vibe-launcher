"""Project page: the drawer listing every workspace (session) a project has.

A project can be opened many times (Codex here, Claude there, a shell on the server...). The list row only has room
for one status, so the details panel carries this drawer: one line per workspace with its own status and actions.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QFont, QIcon, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import icons
from .kit import ElidedLabel, IconButton, StatusBadge, dispose_widget, make_button
from .theme import C, R, S, T, TINT_A, qcolor, resolve, section_hue


@dataclass(frozen=True)
class WorkRow:
    """What the drawer shows for one session. ``tab_id`` is set when a workspace terminal for it is open right now."""

    session_id: str
    title: str
    sub: str
    badge: tuple[str, str]
    tab_id: str = ""
    can_attach: bool = False
    can_stop: bool = False
    can_remove: bool = False
    can_restart: bool = False       # stopped: start it again the same way (same service, account and environment)


class WorkDrawer(QWidget):
    switch_to = Signal(str)       # tab_id: jump to the open terminal
    attach = Signal(str)          # session_id
    stop = Signal(str)            # session_id
    remove = Signal(str)          # session_id
    restart = Signal(str)         # session_id of a stopped workspace
    restore_all = Signal()        # bring every recoverable workspace of the project back
    new_work = Signal()           # open the project once more

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._rows: list[WorkRow] = []
        self._open = True
        root = QVBoxLayout(self)
        root.setContentsMargins(S.sm, S.sm, S.sm, S.sm)
        root.setSpacing(S.xs)
        head = QHBoxLayout()
        head.setSpacing(S.xs)
        self.toggle = make_button("", "ghost", small=True, tip="展開或收合這個專案的工作區")
        self.toggle.clicked.connect(lambda *_: self.set_open(not self._open))
        head.addWidget(self.toggle)
        head.addStretch(1)
        self.add = make_button("再開一個", "subtle", "plus", small=True, tip="用另一個帳號或服務再開啟這個專案")
        self.add.clicked.connect(lambda *_: self.new_work.emit())
        head.addWidget(self.add)
        root.addLayout(head)
        self.summary = QLabel("")
        self.summary.setProperty("role", "meta")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        self.restore = make_button("", "secondary", "repeat", small=True,
                                   tip="一次召回：還在伺服器上執行的接回，已停止的用原本的服務與帳號重新開啟")
        self.restore.clicked.connect(lambda *_: self.restore_all.emit())
        root.addWidget(self.restore, 0, Qt.AlignmentFlag.AlignLeft)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(S.xs)
        root.addWidget(self.body)
        self.set_works([])

    def paintEvent(self, event) -> None:  # noqa: N802
        """A faint wash in the workspace colour marks this block off from the plain facts below it."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        hue = resolve(section_hue("workspace"))
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(qcolor(hue, TINT_A * 0.6))
        p.setPen(QPen(qcolor(hue, 0.20), 1))
        p.drawRoundedRect(rect, R.lg, R.lg)

    # -- state ---------------------------------------------------------------------------------
    def is_open(self) -> bool:
        return self._open

    def set_open(self, value: bool) -> None:
        self._open = bool(value)
        self.body.setVisible(self._open)
        self.toggle.setText("工作區")
        self.toggle.setAccessibleName(("收合" if self._open else "展開") + "工作區")
        self.toggle.setIcon(QIcon(icons.pixmap("chevron-down" if self._open else "chevron-right", C.text_secondary, 14, 2.0)))

    def retheme(self) -> None:
        self.set_open(self._open)

    def rows(self) -> list[WorkRow]:
        return list(self._rows)

    def set_works(self, rows: list[WorkRow], restorable: int = 0) -> None:
        self.restore.setVisible(restorable > 0)
        self.restore.setText(f"召回全部（{restorable}）")
        live = sum(1 for r in rows if r.badge[1] in {"ok", "info", "warn"} or r.can_stop)
        if rows:
            text = f"共 {len(rows)} 個 · 進行中 {live}" + (f" · 已在工作區開啟 {sum(1 for r in rows if r.tab_id)}" if any(r.tab_id for r in rows) else "")
        else:
            text = "尚未開啟過"
        self.summary.setText(text)
        self.set_open(self._open)
        if rows == self._rows and self.body_layout.count():
            return
        self._rows = list(rows)
        while self.body_layout.count():
            widget = self.body_layout.takeAt(0).widget()
            if widget is not None:
                dispose_widget(widget)
        if not rows:
            hint = QLabel("按「啟動終端」開啟；同一個專案可以同時開多個工作區，都會列在這裡。")
            hint.setProperty("role", "meta")
            hint.setWordWrap(True)
            self.body_layout.addWidget(hint)
            return
        for row in rows:
            self.body_layout.addWidget(self._row(row))

    # -- one row -------------------------------------------------------------------------------
    def _row(self, row: WorkRow) -> QWidget:
        frame = QFrame()
        frame.setProperty("surface", "card")
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        frame.setProperty("workRow", row.session_id)
        box = QVBoxLayout(frame)
        box.setContentsMargins(S.sm, S.xs + 2, S.xs + 2, S.xs + 2)
        box.setSpacing(S.xs)
        top = QHBoxLayout()
        top.setSpacing(S.xs)
        top.addWidget(ElidedLabel(row.title, px=T.body, weight=QFont.Weight.DemiBold), 1)
        top.addWidget(StatusBadge(row.badge[0], row.badge[1]))
        box.addLayout(top)
        box.addWidget(ElidedLabel(row.sub, px=T.meta, color="text_muted"))
        actions = QHBoxLayout()
        actions.setSpacing(S.xs)
        if row.tab_id:
            button = make_button("切換", "subtle", "square-terminal", small=True, tip="跳到工作區裡這個已開啟的終端")
            button.clicked.connect(lambda *_, t=row.tab_id: self.switch_to.emit(t))
            button.setProperty("workAction", "switch")
            actions.addWidget(button)
        elif row.can_restart:
            button = make_button("重新開啟", "subtle", "repeat", small=True, tip="用原本的服務、帳號與環境再開一次")
            button.clicked.connect(lambda *_, s=row.session_id: self.restart.emit(s))
            button.setProperty("workAction", "restart")
            actions.addWidget(button)
        elif row.can_attach:
            button = make_button("接回", "subtle", "activity", small=True, tip="重新連線到伺服器上仍在執行的工作")
            button.clicked.connect(lambda *_, s=row.session_id: self.attach.emit(s))
            button.setProperty("workAction", "attach")
            actions.addWidget(button)
        actions.addStretch(1)
        if row.can_stop:
            stop = IconButton("x-circle", "停止這個工作區", size=26, icon_size=15)
            stop.setProperty("workAction", "stop")
            stop.clicked.connect(lambda *_, s=row.session_id: self.stop.emit(s))
            actions.addWidget(stop)
        if row.can_remove:
            gone = IconButton("trash", "移除這筆紀錄", size=26, icon_size=15)
            gone.setProperty("workAction", "remove")
            gone.clicked.connect(lambda *_, s=row.session_id: self.remove.emit(s))
            actions.addWidget(gone)
        if actions.count() > 1:
            box.addLayout(actions)
        return frame
