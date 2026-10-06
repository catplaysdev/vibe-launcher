"""Project page: the drawer listing every workspace (session) a project has.

A project can be opened many times (Codex here, Claude there, a shell on the server...). The list row only has room
for one status, so the details panel carries this drawer: one line per workspace with its own status and actions.
"""

from __future__ import annotations

from .i18n import tr

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import icons
from .kit import ElidedLabel, IconButton, StatusBadge, dispose_widget, make_button
from .theme import C, S, T


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
        # A plain section (heading, one line of summary, then the cards): no frame of its own around the cards.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(S.sm)
        head = QHBoxLayout()
        head.setSpacing(S.xs)
        self.toggle = make_button("", "ghost", small=True, tip=tr('work_drawer.f695cbe6b211'))
        self.toggle.setProperty("drawerHeading", True)
        self.toggle.clicked.connect(lambda *_: self.set_open(not self._open))
        head.addWidget(self.toggle)
        head.addStretch(1)
        self.add = IconButton("plus", tr('work_drawer.f7f3ba7804bd'), size=30, icon_size=16)
        self.add.setAccessibleName(tr('work_drawer.3044fdc9e952'))
        self.add.clicked.connect(lambda *_: self.new_work.emit())
        head.addWidget(self.add)
        root.addLayout(head)
        self.summary = QLabel("")
        self.summary.setProperty("role", "meta")
        self.summary.setWordWrap(True)
        self.summary.setContentsMargins(S.xs, 0, 0, 0)
        root.addWidget(self.summary)
        self.restore = make_button("", "secondary", "repeat", small=True,
                                   tip=tr('work_drawer.f64f69ce473b'))
        self.restore.clicked.connect(lambda *_: self.restore_all.emit())
        root.addWidget(self.restore, 0, Qt.AlignmentFlag.AlignLeft)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(S.sm)
        root.addWidget(self.body)
        self.set_works([])

    # -- state ---------------------------------------------------------------------------------
    def is_open(self) -> bool:
        return self._open

    def set_open(self, value: bool) -> None:
        self._open = bool(value)
        self.body.setVisible(self._open)
        count = len(self._rows)
        self.toggle.setText(tr('work_drawer.1c5ab76e581d') + (f" · {count}" if count else ""))
        self.toggle.setAccessibleName((tr('work_drawer.32605db9ff7b') if self._open else tr('work_drawer.ccd4ff23f93b')) + tr('work_drawer.1c5ab76e581d'))
        self.toggle.setIcon(QIcon(icons.pixmap("chevron-down" if self._open else "chevron-right", C.text_secondary, 14, 2.0)))

    def retheme(self) -> None:
        self.set_open(self._open)

    def rows(self) -> list[WorkRow]:
        return list(self._rows)

    def set_works(self, rows: list[WorkRow], restorable: int = 0) -> None:
        self.restore.setVisible(restorable > 0)
        self.restore.setText(tr('work_drawer.0e8fb1011d11', v0=f'{restorable}'))
        live = sum(1 for r in rows if r.badge[1] in {"ok", "info", "warn"} or r.can_stop)
        if rows:
            text = tr('work_drawer.09d9999c00cf', v0=f'{len(rows)}', v1=f'{live}') + (tr('work_drawer.f3863aa3f25c', v0=f'{sum((1 for r in rows if r.tab_id))}') if any(r.tab_id for r in rows) else "")
        else:
            text = tr('work_drawer.e276681df8d8')
        self.summary.setText(text)
        changed = not (rows == self._rows and self.body_layout.count())
        self._rows = list(rows)
        self.set_open(self._open)
        if not changed:
            return
        while self.body_layout.count():
            widget = self.body_layout.takeAt(0).widget()
            if widget is not None:
                dispose_widget(widget)
        if not rows:
            hint = QLabel(tr('work_drawer.f697c5e1e70b'))
            hint.setProperty("role", "meta")
            hint.setWordWrap(True)
            self.body_layout.addWidget(hint)
            return
        for row in rows:
            self.body_layout.addWidget(self._row(row))

    # -- one row -------------------------------------------------------------------------------
    def _row(self, row: WorkRow) -> QWidget:
        """Two lines: name and status, then where/what it is with the actions at the end of the same line."""
        frame = QFrame()
        frame.setProperty("surface", "well")
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        frame.setProperty("workRow", row.session_id)
        box = QVBoxLayout(frame)
        box.setContentsMargins(S.md, S.sm, S.sm, S.sm)
        box.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(S.sm)
        top.addWidget(ElidedLabel(row.title, px=T.body, weight=QFont.Weight.DemiBold), 1)
        top.addWidget(StatusBadge(row.badge[0], row.badge[1]))
        box.addLayout(top)
        actions = QHBoxLayout()
        actions.setSpacing(S.xs)
        actions.addWidget(ElidedLabel(row.sub, px=T.meta, color="text_muted"), 1)
        if row.tab_id:
            button = make_button(tr('work_drawer.873b0afae9ba'), "subtle", "square-terminal", small=True, tip=tr('work_drawer.9618188bfa7a'))
            button.clicked.connect(lambda *_, t=row.tab_id: self.switch_to.emit(t))
            button.setProperty("workAction", "switch")
            actions.addWidget(button)
        elif row.can_restart:
            button = make_button(tr('work_drawer.d82e4e5145df'), "subtle", "repeat", small=True, tip=tr('work_drawer.3a4c40961bdb'))
            button.clicked.connect(lambda *_, s=row.session_id: self.restart.emit(s))
            button.setProperty("workAction", "restart")
            actions.addWidget(button)
        elif row.can_attach:
            button = make_button(tr('work_drawer.dbce59f5b32e'), "subtle", "activity", small=True, tip=tr('work_drawer.64be21a8ebdb'))
            button.clicked.connect(lambda *_, s=row.session_id: self.attach.emit(s))
            button.setProperty("workAction", "attach")
            actions.addWidget(button)
        if row.can_stop:
            stop = IconButton("x-circle", tr('work_drawer.6357f1a1320a'), size=26, icon_size=15)
            stop.setProperty("workAction", "stop")
            stop.clicked.connect(lambda *_, s=row.session_id: self.stop.emit(s))
            actions.addWidget(stop)
        if row.can_remove:
            gone = IconButton("trash", tr('work_drawer.03ec3d0699a2'), size=26, icon_size=15)
            gone.setProperty("workAction", "remove")
            gone.clicked.connect(lambda *_, s=row.session_id: self.remove.emit(s))
            actions.addWidget(gone)
        box.addLayout(actions)
        return frame
