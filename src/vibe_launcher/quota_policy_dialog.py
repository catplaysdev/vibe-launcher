"""Quota plan editor: a library of saved plans, a block canvas with wiring, an options panel and a preview.

Plans are declarative data (runtime.flow_issues / flow_outcome); the editor only draws and edits that data. Every
connection can be made with the mouse (drag from a branch port, or the right-click "Connect to" menu) and with the
keyboard (block list + "Connect to" boxes in the options panel; Ctrl+arrows move a block). Blocks come in four colour
coded categories that are wired left to right (trigger -> check -> choose account -> outcome); only connections the
rules allow (runtime.flow_link_issue, no loops) are offered or accepted. Before a plan is saved, the accounts it may
switch between are checked (account_check): differences are aligned, accepted or the account leaves the plan."""
from __future__ import annotations

import copy
import time
import uuid

from dataclasses import replace

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout,
                               QLineEdit, QListWidget, QListWidgetItem, QMenu, QScrollArea, QSizePolicy, QSpinBox,
                               QVBoxLayout, QWidget)

from . import account_check, runtime
from .account_check_dialog import AccountCheckDialog, CheckRunner
from .i18n import tr
from .kit import ConfirmDialog, FlowLayout, dispose_widget, label, make_button
from .quota_policy import (AUTO_CONTINUE, TEMPLATES, Policy, PolicyStore, flow_issues, flow_link_issue, flow_outcome,
                           quota_flow, template_plan)
from .theme import TINT_A, C, font, qcolor

NODE_W, NODE_H, PORT = 168, 72, 7
COLUMN, ROW = 210, 92               # tidy layout: one column per step, rows leave room for branch labels
CATEGORIES = {"start": "trigger", "condition": "check", "select": "choose", "handoff": "outcome", "wait": "outcome",
              "notify": "outcome", "keep": "outcome"}
CATEGORY_ORDER = ("trigger", "check", "choose", "outcome")
CATEGORY_HUES = {"trigger": "info", "check": "warning", "choose": "violet", "outcome": "success"}     # theme tokens
POSITIVE = ("yes", "found")         # branches drawn solid in the success colour; the others dashed and neutral


def category(kind: str) -> str:
    return CATEGORIES[kind]


def category_label(name: str) -> str:
    return {"trigger": tr("Trigger"), "check": tr("Check"), "choose": tr("Choose account"), "outcome": tr("Outcome")}[name]


def hue(kind: str) -> str:
    """The colour of a block kind's category, read from the active theme."""
    return getattr(C, CATEGORY_HUES[category(kind)])


def kind_label(kind: str) -> str:
    return {"start": tr("Quota check"), "condition": tr("Condition"), "select": tr("Select account"),
            "handoff": tr("Safe handoff"), "wait": tr("Auto continue"), "notify": tr("Notify only"),
            "keep": tr("Keep current account")}[kind]


def test_label(test: str) -> str:
    return {"low": tr("Low quota?"), "exhausted": tr("Quota exhausted?"),
            "available": tr("Another account available?")}.get(test, tr("Choose a condition"))


def strategy_label(strategy: str) -> str:
    return {"priority": tr("In order"), "round-robin": tr("Round robin"),
            "available-quota": tr("Most quota left")}.get(strategy, strategy)


def template_label(template: str) -> str:
    return {"priority": tr("Take over in order"), "round-robin": tr("Rotate accounts"),
            "available-quota": tr("Most quota first"), AUTO_CONTINUE: tr("Auto continue (same account)")}[template]


def branch_label(branch: str) -> str:
    return {"next": "", "yes": tr("Yes"), "no": tr("No"), "found": tr("Found"), "none": tr("None available")}[branch]


def issue_text(code: str) -> str:
    return {"invalid": tr("This plan cannot be read"),
            "start": tr("A plan has exactly one quota check block"),
            "handoff": tr("A found account goes to the safe handoff, and only a found account does"),
            "test": tr("Choose what this condition checks"),
            "accounts": tr("Choose accounts of this environment"),
            "strategy": tr("Choose how to select an account"),
            "edge": tr("This connection is not allowed"),
            "unconnected": tr("Connect every branch"),
            "cycle": tr("Connections cannot loop back; waiting is scheduled, not looped"),
            "unreachable": tr("This block cannot be reached from the quota check"),
            "unguarded": tr("Switch only after a low-quota or exhausted condition answered yes"),
            "accounts_differ": tr("Accounts of this selection differ; open Check accounts to resolve it")}.get(code, code)


def meaning(plan: Policy) -> dict:
    """What a plan does: everything but where its blocks sit (moving or tidying blocks is no new revision)."""
    data = plan.to_dict()
    data.pop("revision")
    flow = data.get("flow") or {"nodes": [], "edges": []}
    data["flow"] = {"nodes": [{key: value for key, value in node.items() if key not in ("x", "y")} for node in flow["nodes"]],
                    "edges": flow["edges"]}
    return data


def arrange(nodes: list, edges: list) -> dict:
    """Tidy positions {id: (x, y)}: a column per step of the longest path from the quota check, left to right; in a
    column, blocks follow their branches' order (Yes / Found above No / None) and never sit above their first parent.
    Blocks the quota check cannot reach go in a band below. The same graph always gives the same positions."""
    kinds = {node["id"]: node["type"] for node in nodes}
    children = {ident: [] for ident in kinds}
    for edge in edges:
        branches = runtime.FLOW_BRANCHES.get(kinds.get(edge["from"]), ())
        if edge["to"] in kinds and edge["branch"] in branches:
            children[edge["from"]].append((branches.index(edge["branch"]), edge["to"]))
    for value in children.values():
        value.sort()
    start = next((ident for ident, kind in kinds.items() if kind == "start"), None)
    depth = {start: 0} if start else {}
    for _ in range(len(nodes)):                     # longest path; a loop still being edited stops at the block count
        grown = False
        for ident in list(depth):
            for _, target in children[ident]:
                if depth.get(target, -1) < depth[ident] + 1 < len(nodes):
                    depth[target], grown = depth[ident] + 1, True
        if not grown:
            break
    order, stack, parent = [], [start] if start else [], {}
    while stack:                                    # depth first in branch order: a branch's blocks stay together
        ident = stack.pop()
        if ident in order:
            continue
        order.append(ident)
        for _, target in reversed(children[ident]):
            parent.setdefault(target, ident)
            stack.append(target)
    spots, free = {}, {}
    for ident in sorted(order, key=lambda key: (depth[key], order.index(key))):
        column = depth[ident]
        y = max(free.get(column, 0), spots[parent[ident]][1] if parent.get(ident) in spots else 0)
        spots[ident] = (min(column * COLUMN, 10000), min(y, 10000))
        free[column] = y + ROW
    band = max([y for _, y in spots.values()] + [-ROW]) + ROW + ROW // 2
    for index, ident in enumerate(ident for ident in kinds if ident not in spots):
        spots[ident] = (min(index * COLUMN, 10000), min(band, 10000))
    return spots


class Swatch(QWidget):
    """A small square in a category's colour (legend)."""
    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.setFixedSize(12, 12)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(qcolor(getattr(C, CATEGORY_HUES[self.name])))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 3, 3)


class FlowCanvas(QWidget):
    """Draws the blocks and their wires; a block is selected, dragged or wired here."""
    selected = Signal(str)
    changed = Signal()

    def __init__(self, editor: "QuotaPolicyDialog"):
        super().__init__(editor)
        self.editor = editor
        self.current = ""
        self.trace: list = []
        self._drag = None               # ("move", id, offset, start position) or ("wire", id, branch, point)
        self.legal: set = set()         # while wiring: the blocks the dragged branch may connect to
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(tr("Plan blocks"))
        self.setAccessibleDescription(tr("Up and Down select a block, Ctrl+arrows move it, Delete removes it, "
                                         "the menu key opens Connect to."))
        self.setMinimumSize(780, 300)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)

    # geometry
    def rect_of(self, node: dict) -> QRectF:
        return QRectF(node.get("x", 0) + 8, node.get("y", 0) + 8, NODE_W, NODE_H)

    def port(self, node: dict, branch: str) -> QPointF:
        branches = runtime.FLOW_BRANCHES.get(node["type"], ())
        rect = self.rect_of(node)
        step = rect.height() / (len(branches) + 1)
        return QPointF(rect.right(), rect.top() + step * (branches.index(branch) + 1))

    def inlet(self, node: dict) -> QPointF:
        rect = self.rect_of(node)
        return QPointF(rect.left(), rect.center().y())

    def node_at(self, point: QPointF) -> dict | None:
        for node in reversed(self.editor.nodes):
            if self.rect_of(node).adjusted(-2, -2, 2, 2).contains(point):
                return node
        return None

    def port_at(self, point: QPointF):
        for node in self.editor.nodes:
            for branch in runtime.FLOW_BRANCHES.get(node["type"], ()):
                if (self.port(node, branch) - point).manhattanLength() <= PORT + 4:
                    return node, branch
        return None

    def sizeHint(self):  # noqa: N802
        right = max([node.get("x", 0) for node in self.editor.nodes] + [0]) + NODE_W + 60
        bottom = max([node.get("y", 0) for node in self.editor.nodes] + [0]) + NODE_H + 40
        return QSize(max(780, int(right)), max(300, int(bottom)))

    # painting
    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), qcolor(C.surface_sunken))
        nodes = {node["id"]: node for node in self.editor.nodes}
        painter.setFont(font(11))
        traced = []
        for edge in self.editor.edges:
            source, target = nodes.get(edge["from"]), nodes.get(edge["to"])
            if not source or not target or edge["branch"] not in runtime.FLOW_BRANCHES.get(source["type"], ()):
                continue
            start, end = self.port(source, edge["branch"]), self.inlet(target)
            path = QPainterPath(start)
            bend = max(40.0, abs(end.x() - start.x()) / 2)
            path.cubicTo(start + QPointF(bend, 0), end - QPointF(bend, 0), end)
            positive = edge["branch"] in POSITIVE
            pen = QPen(qcolor(C.success if positive else C.text_secondary), 1.8 if positive else 1.5)
            if edge["branch"] in ("no", "none"):
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawPath(path)
            painter.drawLine(end, end + QPointF(-7, -5))
            painter.drawLine(end, end + QPointF(-7, 5))
            if edge["from"] in self.trace and edge["to"] in self.trace:
                traced.append(path)
            text = branch_label(edge["branch"])
            if text:
                painter.setPen(qcolor(C.text_secondary))
                first = runtime.FLOW_BRANCHES[source["type"]].index(edge["branch"]) == 0
                painter.drawText(start + QPointF(10, -6 if first else 16), text)      # above / below its port
        painter.setPen(QPen(qcolor(C.accent), 3))       # the previewed path, over the branch colours
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for path in traced:
            painter.drawPath(path)
        wiring = self._drag[1] if self._drag and self._drag[0] == "wire" else None
        if wiring:
            painter.setPen(QPen(qcolor(C.accent), 2, Qt.PenStyle.DashLine))
            painter.drawLine(self.port(nodes[wiring], self._drag[2]), self._drag[3])
        problems = self.editor.node_issues
        for node in self.editor.nodes:
            self._paint_block(painter, node, node["id"] in problems, wiring)
        if self.hasFocus():
            painter.setPen(QPen(qcolor(C.accent, 0.6), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(self.rect()).adjusted(1, 1, -1, -1))

    def _paint_block(self, painter: QPainter, node: dict, problem: bool, wiring: str | None) -> None:
        rect, color = self.rect_of(node), hue(node["type"])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(qcolor(C.surface_raised))
        painter.drawRoundedRect(rect, 8, 8)
        painter.setBrush(qcolor(color, TINT_A))         # the category's wash ...
        painter.drawRoundedRect(rect, 8, 8)
        painter.setBrush(qcolor(color))                 # ... and its stripe
        painter.drawRoundedRect(QRectF(rect.left(), rect.top(), 5, rect.height()), 2, 2)
        current = node["id"] == self.current
        border = C.danger if problem else (C.accent if current else C.border)
        width = 2.4 if problem or current else 1.0
        painter.setPen(QPen(qcolor(border, 1.0 if width > 1 else 0.35), width))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 8, 8)
        outcome = category(node["type"]) == "outcome"
        painter.setFont(font(10))
        painter.setPen(qcolor(C.text_secondary))
        heading = category_label(category(node["type"])) + (" · " + tr("ends here") if outcome else "")
        painter.drawText(rect.adjusted(14, 5, -24, -(NODE_H - 21)), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(heading, Qt.TextElideMode.ElideRight, int(rect.width()) - 38))
        painter.setPen(qcolor(C.text))
        painter.setFont(font(12, QFont.Weight.DemiBold))
        painter.drawText(rect.adjusted(14, 22, -8, -24), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(self.editor.title_of(node), Qt.TextElideMode.ElideRight,
                                                          int(rect.width()) - 22))
        painter.setFont(font(11))
        painter.setPen(qcolor(C.text_secondary))
        painter.drawText(rect.adjusted(14, NODE_H - 26, -8, -6), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(self.editor.detail_of(node), Qt.TextElideMode.ElideRight,
                                                          int(rect.width()) - 22))
        if problem:
            painter.setPen(qcolor(C.danger))
            painter.setFont(font(13, QFont.Weight.Bold))
            painter.drawText(QRectF(rect.right() - 22, rect.top() + 2, 18, 18), Qt.AlignmentFlag.AlignCenter, "!")
        if node["type"] != "start":                     # every block but the quota check is wired into on its left
            painter.setPen(QPen(qcolor(color), 1.6))
            painter.setBrush(qcolor(C.surface))
            painter.drawEllipse(self.inlet(node), PORT / 2 + 1, PORT / 2 + 1)
        for branch in runtime.FLOW_BRANCHES.get(node["type"], ()):
            connected = any(e["from"] == node["id"] and e["branch"] == branch for e in self.editor.edges)
            tone = C.success if branch in POSITIVE else C.text_secondary
            painter.setPen(QPen(qcolor(tone), 1.6))
            painter.setBrush(qcolor(tone) if connected else qcolor(C.surface))
            painter.drawEllipse(self.port(node, branch), PORT / 2 + 1, PORT / 2 + 1)
        if outcome:                                     # an end has no outlet
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(color))
            painter.drawRect(QRectF(rect.right() - 4, rect.center().y() - 4, 8, 8))
        if wiring and node["id"] != wiring:
            if node["id"] in self.legal:
                painter.setPen(QPen(qcolor(C.accent), 2, Qt.PenStyle.DashLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), 10, 10)
            else:                                       # cannot take this wire: dimmed
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(qcolor(C.surface_sunken, 0.62))
                painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), 8, 8)

    # mouse
    def mousePressEvent(self, event):  # noqa: N802
        point = event.position()
        hit = self.port_at(point)
        if hit and event.button() == Qt.MouseButton.LeftButton:
            self._drag = ("wire", hit[0]["id"], hit[1], point)
            self.legal = {node["id"] for node in self.editor.nodes
                          if self.editor.link_issue(hit[0]["id"], hit[1], node["id"]) is None}
            self.editor.status.setText(tr("Drop the wire on a highlighted block."))
            self.update()
            return
        node = self.node_at(point)
        if node:
            self.editor.select_block(node["id"])
            if event.button() == Qt.MouseButton.LeftButton:
                spot = (node.get("x", 0), node.get("y", 0))
                self._drag = ("move", node["id"], point - QPointF(*spot), spot)
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    def mouseMoveEvent(self, event):  # noqa: N802
        if not self._drag:
            return
        point = event.position()
        if self._drag[0] == "move":
            node = self.editor.block(self._drag[1])
            spot = point - self._drag[2]
            node["x"], node["y"] = max(0, min(10000, int(spot.x()))), max(0, min(10000, int(spot.y())))
            self.updateGeometry()
        else:
            self._drag = self._drag[:3] + (point,)
        self.update()

    def mouseReleaseEvent(self, event):  # noqa: N802
        drag, self._drag = self._drag, None
        if drag and drag[0] == "wire":
            self.legal = set()
            target = self.node_at(event.position())
            if target and target["id"] != drag[1]:
                self.editor.connect_branch(drag[1], drag[2], target["id"])
            else:
                self.editor.show_check()
        elif drag:
            node = self.editor.block(drag[1])
            if node and (node.get("x", 0), node.get("y", 0)) != drag[3]:
                self.changed.emit()                     # a click that moved nothing changes nothing
        self.update()

    def contextMenuEvent(self, event):  # noqa: N802
        node = self.node_at(QPointF(event.pos())) or self.editor.block(self.current)
        if node:
            self.editor.select_block(node["id"])
            self.editor.connect_menu(node).exec(event.globalPos())

    # keyboard
    def keyPressEvent(self, event):  # noqa: N802
        key, node = event.key(), self.editor.block(self.current)
        moves = {Qt.Key.Key_Left: (-20, 0), Qt.Key.Key_Right: (20, 0), Qt.Key.Key_Up: (0, -20), Qt.Key.Key_Down: (0, 20)}
        if node and key in moves and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            dx, dy = moves[key]
            node["x"], node["y"] = max(0, min(10000, node.get("x", 0) + dx)), max(0, min(10000, node.get("y", 0) + dy))
            self.updateGeometry()
            self.update()
            self.changed.emit()
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and self.editor.nodes:
            ids = [item["id"] for item in self.editor.nodes]
            index = ids.index(self.current) if self.current in ids else -1
            index = max(0, min(len(ids) - 1, index + (1 if key == Qt.Key.Key_Down else -1)))
            self.editor.select_block(ids[index])
        elif key == Qt.Key.Key_Delete and node:
            self.editor.delete_block()
        elif key == Qt.Key.Key_Menu and node:
            self.editor.connect_menu(node).exec(self.mapToGlobal(self.rect_of(node).bottomLeft().toPoint()))
        else:
            super().keyPressEvent(event)


class QuotaPolicyDialog(QDialog):
    """Saved plans of one environment and CLI. ``result_plan`` is the plan to apply when the dialog is accepted.

    ``checker(accounts, reference_id)`` returns an account_check.Report for the accounts of the edited plan (with the
    terminal's own account ``own_account``); it runs off the GUI thread. Without one (an SSH host, or no data folder)
    the accounts are checked on the host at each handoff instead."""
    accountsChecked = Signal()

    def __init__(self, parent=None, accounts=None, environment_id="local", tool="codex", store: PolicyStore | None = None,
                 current_plan_id: str = "", snapshots=None, *, own_account: str = "", session_id: str = "",
                 checker=None, aligner=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Quota plans"))
        self.resize(1320, 720)
        self.environment_id, self.tool, self.store = environment_id, tool, store
        self.accounts = [account for account in (accounts or []) if account.get("tool") == tool
                         and runtime.switch_supported(account) and runtime.account_fits(account, environment_id)]
        self.names = {account["id"]: account.get("name") or account["id"] for account in self.accounts}
        self.snapshots = snapshots or {}
        self.result_plan: Policy | None = None
        self.plans: dict[str, Policy] = {}
        self.plan_id = ""
        self.nodes: list = []
        self.edges: list = []
        self.node_issues: dict = {}
        self.issues: list = []
        self._loading = False
        self.own_account, self.session_id = own_account, session_id
        self.checker, self.aligner = checker, aligner
        self.accepted: dict = {}                # account id -> {"plugin:name", ...} the user accepted to lose
        self.reference = own_account
        self.report: account_check.Report | None = None
        self.check_state = "unavailable" if checker is None else "pending"
        self.check_error = ""
        self._check_key = None
        self._runner = CheckRunner(self)
        self._runner.done.connect(self._checked)
        self._check_timer = QTimer(self)        # owned by the dialog: debounces edits of the account pools
        self._check_timer.setSingleShot(True)
        self._check_timer.timeout.connect(self._start_check)
        self._build()
        self.reload(current_plan_id)

    # --- layout
    def _build(self):
        outer = QHBoxLayout(self)
        left = QVBoxLayout()
        left.addWidget(label(tr("Saved plans"), "heading"))
        self.plan_list = QListWidget()
        self.plan_list.setAccessibleName(tr("Saved plans"))
        self.plan_list.currentItemChanged.connect(self._plan_chosen)
        left.addWidget(self.plan_list, 1)
        self.new_button = make_button(tr("New plan"), "secondary", "plus")
        menu = QMenu(self.new_button)
        for template in TEMPLATES:
            menu.addAction(template_label(template), lambda template=template: self.new_plan(template))
        menu.addSeparator()
        menu.addAction(template_label(AUTO_CONTINUE), lambda: self.new_plan(AUTO_CONTINUE))
        self.new_button.setMenu(menu)
        left.addWidget(self.new_button)
        self.save_as_button = make_button(tr("Save as copy"), "secondary", callback=self.save_as)
        self.rename_button = make_button(tr("Rename"), "secondary", callback=self.rename)
        self.delete_button = make_button(tr("Delete"), "danger", callback=self.delete_plan)
        for button in (self.save_as_button, self.rename_button, self.delete_button):
            left.addWidget(button)
        side = QWidget()
        side.setLayout(left)
        side.setFixedWidth(200)
        outer.addWidget(side)

        middle = QVBoxLayout()
        header = QFormLayout()
        self.name = QLineEdit()
        self.name.setAccessibleName(tr("Plan name"))
        self.name.setMaxLength(64)
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0, 99)
        self.threshold.setSuffix(" %")
        self.threshold.setAccessibleName(tr("Low quota threshold"))
        self.interval = QSpinBox()
        self.interval.setRange(60, 86400)
        self.interval.setSuffix(tr(" seconds"))
        self.interval.setAccessibleName(tr("Quota check interval"))
        accounts_row = QHBoxLayout()
        self.account_status = label("", wrap=True)
        self.account_status.setAccessibleName(tr("Account check result"))
        self.check_button = make_button(tr("Check accounts…"), "secondary", small=True, callback=self.open_account_check)
        accounts_row.addWidget(self.account_status, 1)
        accounts_row.addWidget(self.check_button)
        row = QHBoxLayout()
        row.addWidget(self.threshold)
        row.addWidget(label(tr("check every")))
        row.addWidget(self.interval)
        header.addRow(tr("Plan name"), self.name)
        header.addRow(tr("Low quota at or below"), row)
        header.addRow(tr("Accounts"), accounts_row)
        middle.addLayout(header)
        # The legend is the palette: each category's colour, name and the blocks it adds (the quota check and the safe
        # handoff are managed by the editor).
        palette = QWidget()
        flow = FlowLayout(palette)
        adds = {"check": (("condition", tr("Add condition")),), "choose": (("select", tr("Add account selection")),),
                "outcome": (("wait", tr("Add auto continue")), ("notify", tr("Add notification")), ("keep", tr("Add keep")))}
        self.add_buttons = {}
        for name in CATEGORY_ORDER:
            group = QWidget()                           # a category wraps as one piece
            row = QHBoxLayout(group)
            row.setContentsMargins(0, 0, 12, 0)
            row.addWidget(Swatch(name))
            row.addWidget(label(category_label(name), "meta"))
            for kind, text in adds.get(name, ()):
                button = make_button(text, "ghost", "plus", small=True, tint=CATEGORY_HUES[name],
                                     callback=lambda checked=False, kind=kind: self.add_block(kind))
                self.add_buttons[kind] = button
                row.addWidget(button)
            flow.addWidget(group)
        self.tidy_button = make_button(tr("Tidy layout"), "ghost", small=True, callback=self.tidy,
                                       tip=tr("Arrange the blocks from left to right by their order in the plan"))
        flow.addWidget(self.tidy_button)
        middle.addWidget(palette)
        self.rules = label(tr("Wire from left to right: Trigger → Check → Choose account → Outcome. Green wires are "
                              "Yes or Found, dashed ones No or None available; an account selection always ends in "
                              "the safe handoff, and outcomes end the plan."), "meta", wrap=True)
        middle.addWidget(self.rules)
        self.canvas = FlowCanvas(self)
        self.canvas.changed.connect(self._changed)
        scroll = QScrollArea()
        scroll.setWidget(self.canvas)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        middle.addWidget(scroll, 1)
        self.status = label("", wrap=True)
        self.status.setAccessibleName(tr("Plan check and preview result"))
        middle.addWidget(self.status)
        preview = QHBoxLayout()
        preview.addWidget(label(tr("Preview with")))
        self.preview_current = QComboBox()
        self.preview_current.setAccessibleName(tr("Current account in the preview"))
        preview.addWidget(self.preview_current)
        self.preview_quota = {}
        for account in self.accounts:
            spin = QSpinBox()
            spin.setRange(-1, 100)
            spin.setSpecialValueText(tr("unknown"))
            spin.setSuffix(" %")
            spin.setPrefix(self.names[account["id"]] + " ")
            spin.setAccessibleName(tr("Quota left for {name} in the preview", name=self.names[account["id"]]))
            spin.setValue(self._known_quota(account["id"]))
            self.preview_quota[account["id"]] = spin
            preview.addWidget(spin)
            self.preview_current.addItem(self.names[account["id"]], account["id"])
        self.preview_button = make_button(tr("Preview"), "secondary", callback=self.preview)
        preview.addWidget(self.preview_button)
        preview.addStretch(1)
        middle.addLayout(preview)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.save_button = make_button(tr("Save"), "secondary", callback=self.save)
        self.apply_button = make_button(tr("Save and apply to this terminal"), "primary", callback=self.apply)
        close = make_button(tr("Close"), "ghost", callback=self.reject)
        for button in (self.save_button, self.apply_button, close):
            actions.addWidget(button)
        middle.addLayout(actions)
        outer.addLayout(middle, 1)

        right = QVBoxLayout()
        right.addWidget(label(tr("Blocks"), "heading"))
        self.block_list = QListWidget()
        self.block_list.setAccessibleName(tr("Blocks of this plan"))
        self.block_list.currentItemChanged.connect(
            lambda item, _old: item is not None and not self._loading and self.select_block(item.data(Qt.ItemDataRole.UserRole)))
        right.addWidget(self.block_list, 1)
        self.options = QVBoxLayout()
        holder = QWidget()
        holder.setLayout(self.options)
        holder.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)     # scrolls, never squeezed
        options = QScrollArea()                         # an account selection's options are taller than the panel
        options.setWidget(holder)
        options.setWidgetResizable(True)
        options.setFrameShape(QFrame.Shape.NoFrame)
        options.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right.addWidget(options, 2)
        self.delete_block_button = make_button(tr("Delete block"), "ghost", small=True, callback=self.delete_block)
        right.addWidget(self.delete_block_button)
        panel = QWidget()
        panel.setLayout(right)
        panel.setFixedWidth(280)
        outer.addWidget(panel)
        self.name.textEdited.connect(self._changed)
        self.threshold.valueChanged.connect(self._changed)
        self.interval.valueChanged.connect(self._changed)

    def _known_quota(self, account_id: str) -> int:
        remaining = runtime.quota_state(self.snapshots.get(account_id) or {}, self.environment_id, time.time(), 120)[0]
        return -1 if remaining is None else int(remaining)

    # --- plans
    def reload(self, choose: str = ""):
        self.plans = {}
        if self.store is not None:
            try:
                self.plans = {key: plan for key, plan in self.store.load().items()
                              if plan.environment_id == self.environment_id and set(plan.account_ids) <= set(self.names)}
            except (OSError, ValueError) as error:
                self.status.setText(tr("Saved plans could not be read: {error}", error=str(error)))
        self.plan_list.blockSignals(True)
        self.plan_list.clear()
        for plan in self.plans.values():
            item = QListWidgetItem(plan.name)
            item.setData(Qt.ItemDataRole.UserRole, plan.id)
            self.plan_list.addItem(item)
        self.plan_list.blockSignals(False)
        if choose in self.plans:
            self.plan_list.setCurrentRow(list(self.plans).index(choose))
        elif self.plans:
            self.plan_list.setCurrentRow(0)
        elif self.accounts:
            self.new_plan("priority")
        self._update_buttons()

    def _plan_chosen(self, item, _old=None):
        if item is not None:
            self.load_plan(self.plans[item.data(Qt.ItemDataRole.UserRole)])

    def load_plan(self, plan: Policy):
        self._loading = True
        self.plan_id = plan.id
        self.name.setText(plan.name)
        self.threshold.setValue(plan.threshold)
        self.interval.setValue(plan.interval)
        self.accepted = {key: set(value) for key, value in (plan.accepted_gaps or {}).items()}
        flow = copy.deepcopy(quota_flow(plan))
        self.nodes, self.edges = flow["nodes"], flow["edges"]
        self._loading = False
        self.canvas.trace = []
        self._check_key = None                  # another plan: check its accounts afresh
        self._refresh(select=self.nodes[0]["id"] if self.nodes else "")
        self._queue_check(delay=0)

    def new_plan(self, template: str):
        plan_id = uuid.uuid4().hex
        pool = [account["id"] for account in self.accounts]
        if not pool:
            self.status.setText(tr("No subscription accounts of this CLI in this environment"))
            return
        plan = template_plan(plan_id, template_label(template), template, self.environment_id, pool)
        self.plan_list.clearSelection()
        self.load_plan(plan)

    def current_plan(self) -> Policy:
        """The edited plan; raises ValueError (with what to fix) when it cannot run. Its revision grows only when what
        it does changed since it was saved."""
        if self.issues:
            raise ValueError(issue_text(self.issues[0][1]))
        pools = [item for node in self.nodes if node["type"] == "select" for item in node["accounts"]]
        watched = list(dict.fromkeys(pools)) or list(self.names)
        old = self.plans.get(self.plan_id)
        plan = runtime.checked_quota_policy({
            "id": self.plan_id, "plan_id": self.plan_id, "name": self.name.text().strip() or tr("Quota plan"),
            "environment_id": self.environment_id, "account_ids": watched, "threshold": self.threshold.value(),
            "interval": self.interval.value(), "accepted_gaps": self.accepted_dict(),
            "revision": old.revision if old else 1,
            "flow": {"nodes": copy.deepcopy(self.nodes), "edges": copy.deepcopy(self.edges)}})
        return replace(plan, revision=old.revision + 1) if old and meaning(old) != meaning(plan) else plan

    def _store_plan(self, plan: Policy) -> bool:
        if self.store is None:
            return True
        try:
            plans = self.store.load()
            plans.pop(plan.id, None)
            plans[plan.id] = plan
            self.store.save(plans)
        except (OSError, ValueError) as error:
            self.status.setText(tr("The plan could not be saved: {error}", error=str(error)))
            return False
        return True

    def save(self) -> bool:
        if not self.accounts_ready():
            self.show_check()
            return False
        try:
            plan = self.current_plan()
        except ValueError as error:
            self.status.setText(str(error))
            return False
        if not self._store_plan(plan):
            return False
        self.reload(plan.id)
        self.status.setText(tr("Plan saved. Terminals that use it change only when you apply it again."))
        return True

    def save_as(self):
        self.plan_id = uuid.uuid4().hex
        self.name.setText(tr("{name} (copy)", name=self.name.text().strip() or tr("Quota plan"))[:64])
        self.save()

    def rename(self):
        self.name.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.name.selectAll()
        self.status.setText(tr("Type the new name, then save."))

    def delete_plan(self):
        plan = self.plans.get(self.plan_id)
        if plan is None or self.store is None:
            return
        if not self._confirm(tr("Delete plan"), tr("Delete {name}? Terminals keep the copy they applied.", name=plan.name)):
            return
        try:
            plans = self.store.load()
            plans.pop(plan.id, None)
            self.store.save(plans)
        except (OSError, ValueError) as error:
            self.status.setText(tr("The plan could not be saved: {error}", error=str(error)))
            return
        self.reload()

    def _confirm(self, title: str, message: str) -> bool:
        return ConfirmDialog.ask(self, title, message, confirm_text=tr("Delete"), danger=True)

    def apply(self):
        if self.save():
            self.result_plan = self.plans.get(self.plan_id)
            self.accept()

    # --- blocks
    def block(self, ident: str) -> dict | None:
        return next((node for node in self.nodes if node["id"] == ident), None)

    def title_of(self, node: dict) -> str:
        return test_label(node.get("test", "")) if node["type"] == "condition" else kind_label(node["type"])

    def detail_of(self, node: dict) -> str:
        if node["type"] == "select":
            return strategy_label(node.get("strategy", "")) + " · " + ", ".join(self.names.get(a, a) for a in node.get("accounts", []))
        if node["type"] == "wait":
            return (tr("Claude continues by itself after the reset") if self.tool == "claude"
                    else tr("Continues after a verified reset"))
        return {"start": tr("Runs at every quota check"), "condition": kind_label("condition"),
                "handoff": tr("Switches at a verified safe point"), "notify": tr("You continue by hand"),
                "keep": tr("Nothing changes")}[node["type"]]

    def explanation_of(self, node: dict) -> str:
        """The longer text of the options panel."""
        if node["type"] == "select":
            return tr("Healthy conversations keep their account.")
        if node["type"] == "wait":
            return (tr("Claude Code continues the task by itself after the reset (its setting Continue automatically "
                       "at usage limit, on unless you turned it off; your choice in ~/.claude applies to every account). "
                       "Launcher never restarts Claude for this: if it has not continued two minutes after the reset, "
                       "continue in the terminal. After a safe handoff the task continues on the other account.")
                    if self.tool == "claude" else
                    tr("When quota ran out, Launcher waits for the reset, checks the quota again and then continues "
                       "the unfinished task at a safe point. After a safe handoff the task continues too."))
        return self.detail_of(node)

    def _spot(self, anchor: dict | None) -> tuple[int, int]:
        """A free place for a new block: right of ``anchor`` (or of every block), lower until it overlaps none."""
        if anchor:
            x, y = anchor.get("x", 0) + COLUMN, anchor.get("y", 0)
        else:
            x, y = max([node.get("x", 0) for node in self.nodes] + [-COLUMN]) + COLUMN, 0
        x = min(x, 10000)
        while y < 10000 and any(abs(node.get("x", 0) - x) < NODE_W + 12 and abs(node.get("y", 0) - y) < NODE_H + 12
                                for node in self.nodes):
            y += ROW
        return x, min(y, 10000)

    def add_block(self, kind: str):
        x, y = self._spot(self.block(self.canvas.current))
        node = {"id": kind + "-" + uuid.uuid4().hex[:6], "type": kind, "x": x, "y": y}
        if kind == "condition":
            node["test"] = "low"
        if kind == "select":
            node.update(accounts=list(self.names), strategy="priority")
        self.nodes.append(node)
        if kind == "select":
            # A found account always goes through the one safe handoff: the first selection brings it.
            handoff = next((other for other in self.nodes if other["type"] == "handoff"), None)
            if handoff is None:
                handoff = {"id": "handoff-" + uuid.uuid4().hex[:6], "type": "handoff"}
                handoff["x"], handoff["y"] = self._spot(node)
                self.nodes.append(handoff)
            self.edges.append({"from": node["id"], "branch": "found", "to": handoff["id"]})
        self._changed()
        self.select_block(node["id"])

    def delete_block(self):
        node = self.block(self.canvas.current)
        if not node:
            return
        if node["type"] in ("start", "handoff"):
            self.status.setText(tr("The quota check starts every plan") if node["type"] == "start"
                                else tr("The safe handoff comes and goes with account selection"))
            return
        gone = {node["id"]}
        if node["type"] == "select" and not any(other["type"] == "select" and other is not node for other in self.nodes):
            gone |= {other["id"] for other in self.nodes if other["type"] == "handoff"}     # the last selection's
        self.nodes = [other for other in self.nodes if other["id"] not in gone]
        self.edges = [edge for edge in self.edges if edge["from"] not in gone and edge["to"] not in gone]
        self._changed()
        self.select_block(self.nodes[0]["id"] if self.nodes else "")

    def link_issue(self, source: str, branch: str, target: str) -> str | None:
        """Why ``branch`` of ``source`` may not lead to ``target``: an issue code ("edge", "handoff", "cycle") or None.
        The branch's present connection is left out, as connecting replaces it."""
        nodes = {node["id"]: node for node in self.nodes}
        if source not in nodes or target not in nodes:
            return "edge"
        code = flow_link_issue(nodes[source]["type"], branch, nodes[target]["type"], source == target)
        if code:
            return code
        following = {}
        for edge in self.edges:
            if not (edge["from"] == source and edge["branch"] == branch):
                following.setdefault(edge["from"], []).append(edge["to"])
        stack, seen = [target], set()
        while stack:
            ident = stack.pop()
            if ident == source:
                return "cycle"
            if ident not in seen:
                seen.add(ident)
                stack.extend(following.get(ident, []))
        return None

    def targets(self, node: dict, branch: str) -> list:
        """(block, allowed) a branch can be connected to: every allowed block, and its present target even if not."""
        current = next((e["to"] for e in self.edges if e["from"] == node["id"] and e["branch"] == branch), "")
        allowed = [(other, self.link_issue(node["id"], branch, other["id"]) is None) for other in self.nodes]
        return [(other, ok) for other, ok in allowed if ok or other["id"] == current]

    def target_text(self, other: dict, allowed: bool) -> str:
        text = category_label(category(other["type"])) + " · " + self.title_of(other) + " · " + other["id"]
        return text if allowed else tr("{name} (not allowed)", name=text)

    def connect_branch(self, source: str, branch: str, target: str | None) -> bool:
        """Connect (or, with None, disconnect) a branch. A connection the rules refuse changes nothing and says why."""
        if target:
            code = self.link_issue(source, branch, target)
            if code:
                self.status.setText(tr("Cannot connect: {reason}", reason=issue_text(code)))
                return False
        self.edges = [edge for edge in self.edges if not (edge["from"] == source and edge["branch"] == branch)]
        if target:
            self.edges.append({"from": source, "branch": branch, "to": target})
        self._changed()
        return True

    def connect_menu(self, node: dict) -> QMenu:
        menu = QMenu(self)
        for branch in runtime.FLOW_BRANCHES.get(node["type"], ()):
            sub = menu.addMenu(tr("Connect {branch} to", branch=branch_label(branch) or tr("next")))
            for other, allowed in self.targets(node, branch):
                action = sub.addAction(self.target_text(other, allowed),
                                       lambda branch=branch, other=other["id"]: self.connect_branch(node["id"], branch, other))
                action.setEnabled(allowed)
        if node["type"] not in ("start", "handoff"):
            menu.addSeparator()
            menu.addAction(tr("Delete block"), self.delete_block)
        return menu

    def tidy(self):
        spots, moved = arrange(self.nodes, self.edges), False
        for node in self.nodes:
            if (node.get("x", 0), node.get("y", 0)) != spots[node["id"]]:
                (node["x"], node["y"]), moved = spots[node["id"]], True
        if moved:
            self._changed()

    def select_block(self, ident: str):
        self.canvas.current = ident
        self._loading = True
        for row in range(self.block_list.count()):
            if self.block_list.item(row).data(Qt.ItemDataRole.UserRole) == ident:
                self.block_list.setCurrentRow(row)
        self._loading = False
        self._show_options()
        self.canvas.update()

    def _show_options(self):
        while self.options.count():
            item = self.options.takeAt(0)
            if item.widget():
                dispose_widget(item.widget())
            elif item.layout():
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    if child.widget():
                        dispose_widget(child.widget())
        node = self.block(self.canvas.current)
        self.connect_boxes = {}
        self.delete_block_button.setEnabled(bool(node) and node["type"] not in ("start", "handoff"))
        if not node:
            return
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.options.addLayout(form)
        form.addRow(label(kind_label(node["type"]), "heading"))
        form.addRow(label(category_label(category(node["type"])), "meta"))
        if node["type"] == "condition":
            test = QComboBox()
            test.setAccessibleName(tr("What this condition checks"))
            for value in runtime.FLOW_TESTS:
                test.addItem(test_label(value), value)
            test.setCurrentIndex(max(0, test.findData(node.get("test"))))
            test.currentIndexChanged.connect(lambda _i, node=node, test=test: self._set(node, "test", test.currentData()))
            form.addRow(tr("Checks"), test)
        if node["type"] == "select":
            strategy = QComboBox()
            strategy.setAccessibleName(tr("How to select an account"))
            for value in runtime.FLOW_STRATEGIES:
                strategy.addItem(strategy_label(value), value)
            strategy.setCurrentIndex(max(0, strategy.findData(node.get("strategy"))))
            strategy.currentIndexChanged.connect(lambda _i, node=node, box=strategy: self._set(node, "strategy", box.currentData()))
            form.addRow(tr("Strategy"), strategy)
            pool = QListWidget()
            pool.setAccessibleName(tr("Accounts to select from; order is the priority"))
            pool.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
            chosen = list(node.get("accounts", []))
            for identifier in chosen + [a for a in self.names if a not in chosen]:
                item = QListWidgetItem(self.names.get(identifier, identifier))
                item.setData(Qt.ItemDataRole.UserRole, identifier)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if identifier in chosen else Qt.CheckState.Unchecked)
                pool.addItem(item)
            pool.ensurePolished()                       # the style decides the row height
            pool.setFixedHeight(min(140, pool.sizeHintForRow(0) * pool.count() + 2 * pool.frameWidth() + 4))

            def store_pool(*_args, node=node, pool=pool):
                node["accounts"] = [pool.item(i).data(Qt.ItemDataRole.UserRole) for i in range(pool.count())
                                    if pool.item(i).checkState() == Qt.CheckState.Checked]
                self._changed()
            pool.itemChanged.connect(store_pool)
            pool.model().rowsMoved.connect(store_pool)
            form.addRow(tr("Accounts"), pool)
            order = QHBoxLayout()
            for text, delta in ((tr("Move up"), -1), (tr("Move down"), 1)):
                def move(checked=False, delta=delta, pool=pool):
                    row = pool.currentRow()
                    if row >= 0 and 0 <= row + delta < pool.count():
                        pool.insertItem(row + delta, pool.takeItem(row))
                        pool.setCurrentRow(row + delta)
                        store_pool()
                order.addWidget(make_button(text, "ghost", small=True, callback=move))
            form.addRow("", order)
        form.addRow(label(self.explanation_of(node), wrap=True))
        branches = runtime.FLOW_BRANCHES.get(node["type"], ())
        for branch in branches:
            box = QComboBox()
            box.setAccessibleName(tr("Connect {branch} to", branch=branch_label(branch) or tr("next")))
            box.addItem(tr("Not connected"), "")
            for other, allowed in self.targets(node, branch):
                box.addItem(self.target_text(other, allowed), other["id"])
            current = next((e["to"] for e in self.edges if e["from"] == node["id"] and e["branch"] == branch), "")
            box.setCurrentIndex(max(0, box.findData(current)))
            self.connect_boxes[branch] = box
            box.currentIndexChanged.connect(
                lambda _i, branch=branch, box=box, node=node: self.connect_branch(node["id"], branch, box.currentData() or None))
            form.addRow(tr("Connect {branch} to", branch=branch_label(branch) or tr("next")), box)
        if branches:
            form.addRow(label(tr("Only blocks this branch may connect to are listed."), "meta", wrap=True))
        problems = self.node_issues.get(node["id"], [])
        if problems:
            warning = label("\n".join("• " + issue_text(code) for code in problems), wrap=True)
            warning.setStyleSheet("color: %s;" % C.danger)
            form.addRow(warning)

    def _set(self, node: dict, key: str, value):
        node[key] = value
        self._changed()

    def _changed(self, *_args):
        if self._loading:
            return
        self.canvas.trace = []
        self._refresh(select=self.canvas.current)
        self._queue_check()

    def show_check(self):
        """The status line says what keeps the plan from saving, or that it is valid."""
        if self.issues:
            self.status.setText(tr("Fix before saving:") + " " + "; ".join(dict.fromkeys(issue_text(code) for _, code in self.issues)))
        elif not self.accounts_ready():
            self.status.setText(self.account_summary())
        else:
            self.status.setText(tr("The plan is valid. Preview it with made-up quota; no work starts."))

    # --- the accounts the plan may switch between (account_check)
    def check_set(self) -> list:
        """The terminal's own account and every account of a selection, in that order."""
        records = {account["id"]: account for account in self.accounts}
        ids = [self.own_account] + [item for node in self.nodes if node["type"] == "select" for item in node.get("accounts", [])]
        return [records[item] for item in dict.fromkeys(ids) if item in records]

    def accepted_dict(self) -> dict:
        ids = {account["id"] for account in self.check_set()}
        return {key: sorted(value) for key, value in self.accepted.items() if value and key in ids}

    def open_findings(self) -> list:
        if self.check_state != "done" or self.report is None:
            return []
        return self.report.open(self.accepted_dict())

    def accounts_ready(self) -> bool:
        return self.check_state in ("unavailable", "single") or (self.check_state == "done" and not self.open_findings())

    def account_summary(self) -> str:
        state, count = self.check_state, len(self.open_findings())
        if state == "unavailable":
            return tr("Accounts on an SSH host are checked there at each handoff.")
        if state == "single":
            return tr("One account: nothing to compare.")
        if state in ("pending", "checking"):
            return tr("Checking the accounts of this plan…")
        if state == "failed":
            return tr("The accounts could not be checked: {error}", error=self.check_error)
        if count:
            return tr("{count} account differences to resolve before saving; open Check accounts.", count=count)
        return tr("Every account can take over the others' conversations.")

    def _queue_check(self, force: bool = False, delay: int = 400) -> None:
        if self.checker is None:
            self.account_status.setText(self.account_summary())
            self.check_button.setEnabled(False)
            return
        accounts = self.check_set()
        if self.reference not in {account["id"] for account in accounts}:
            self.reference = accounts[0]["id"] if accounts else ""
        key = (tuple(account["id"] for account in accounts), self.reference)
        if key == self._check_key and not force:
            return
        self._check_key = key
        if len(accounts) < 2:
            self._runner.generation += 1        # an answer still on its way no longer applies
            self.check_state, self.report = "single", None
            self._account_state_changed()
            return
        self.check_state = "checking"
        self._account_state_changed()
        self._check_timer.start(delay)

    def _start_check(self) -> None:
        accounts, reference, checker = self.check_set(), self.reference, self.checker
        if len(accounts) >= 2:
            self._runner.run(lambda: checker(accounts, reference))

    def _checked(self, generation: int, report, error) -> None:
        if generation != self._runner.generation or self.check_state != "checking":
            return                              # the pool or the reference changed since
        if error is not None:
            self.check_state, self.check_error, self.report = "failed", str(error)[:200], None
        else:
            self.check_state, self.report = "done", report
        self._account_state_changed()

    def _account_state_changed(self) -> None:
        self.account_status.setText(self.account_summary())
        warn = self.check_state == "failed" or bool(self.open_findings())
        self.account_status.setStyleSheet("color: %s;" % (C.warning if warn else C.text_secondary))
        self.check_button.setEnabled(self.checker is not None)
        if not self._loading:
            self._refresh(select=self.canvas.current)
        self.accountsChecked.emit()

    def recheck(self) -> None:
        self._queue_check(force=True, delay=0)

    def set_reference(self, identifier: str) -> None:
        self.reference = identifier
        self._queue_check()

    def accept_gap(self, identifier: str, key: str, accepted: bool) -> None:
        (self.accepted.setdefault(identifier, set()).add if accepted else self.accepted.get(identifier, set()).discard)(key)
        self._changed()
        self.accountsChecked.emit()

    def remove_account(self, identifier: str) -> bool:
        """Take an account out of every selection of the plan (the terminal's own account cannot leave)."""
        if identifier == self.own_account:
            return False
        for node in self.nodes:
            if node["type"] == "select":
                node["accounts"] = [item for item in node.get("accounts", []) if item != identifier]
        self.accepted.pop(identifier, None)
        self._changed()
        return True

    def align_account(self, identifier: str) -> bool:
        records = {account["id"]: account for account in self.accounts}
        target, reference = records.get(identifier), records.get(self.reference)
        if self.aligner is None or target is None or reference is None:
            return False
        message = tr("Give {target} the approval rules, extension files and shared settings of {reference}? Its current "
                     "files are backed up first (alignment-backups in the launcher data folder). Sign-ins, plugins and "
                     "trusted folders stay as they are.", target=self.names[identifier], reference=self.names[self.reference])
        if not self._confirm_align(tr("Align accounts"), message):
            return False
        try:
            changed = self.aligner(target, reference)
        except (OSError, ValueError, RuntimeError) as error:
            self.status.setText(tr("{account} could not be aligned: {error}", account=self.names[identifier], error=str(error)))
            return False
        self.status.setText(tr("{account} now matches {reference} ({count} items).", account=self.names[identifier],
                               reference=self.names[self.reference], count=len(changed)))
        self.recheck()
        return True

    def _confirm_align(self, title: str, message: str) -> bool:
        return ConfirmDialog.ask(self, title, message, confirm_text=tr("Align"))

    def open_account_check(self) -> None:
        if self.check_state in ("pending", "failed"):
            self.recheck()
        AccountCheckDialog(self).exec()

    def _refresh(self, select: str = ""):
        self.issues = flow_issues({"nodes": self.nodes, "edges": self.edges}, tuple(self.names))
        self.node_issues = {}
        for ident, code in self.issues:
            self.node_issues.setdefault(ident, []).append(code)
        differing = {finding.account_id for finding in self.open_findings()}
        for node in self.nodes:
            if node["type"] == "select" and differing & set(node.get("accounts", [])):
                self.node_issues.setdefault(node["id"], []).append("accounts_differ")
        self._loading = True
        self.block_list.clear()
        for node in self.nodes:
            item = QListWidgetItem(("⚠ " if node["id"] in self.node_issues else "")
                                   + category_label(category(node["type"])) + " · " + self.title_of(node) + " · " + node["id"])
            item.setData(Qt.ItemDataRole.UserRole, node["id"])
            self.block_list.addItem(item)
        self._loading = False
        self.show_check()
        self.select_block(select if self.block(select) else (self.nodes[0]["id"] if self.nodes else ""))
        self.canvas.updateGeometry()
        self.canvas.adjustSize()
        self._update_buttons()

    def _update_buttons(self):
        valid = not self.issues and bool(self.nodes) and self.accounts_ready()
        for button in (self.save_button, self.apply_button, self.save_as_button, self.preview_button):
            button.setEnabled(valid)
        self.delete_button.setEnabled(self.plan_id in self.plans)
        self.rename_button.setEnabled(bool(self.nodes))
        self.tidy_button.setEnabled(bool(self.nodes))

    # --- preview: made-up quota, nothing starts
    def preview(self) -> tuple[str, str | None]:
        try:
            plan = self.current_plan()
        except ValueError as error:
            self.status.setText(str(error))
            return "invalid", None
        moment = time.time()
        current = self.preview_current.currentData()
        snapshots = {}
        for identifier, spin in self.preview_quota.items():
            if spin.value() >= 0:
                snapshots[identifier] = {"tool": self.tool, "account_id": identifier, "environment_id": self.environment_id,
                                         "checked_at": moment, "windows": [{"used_percent": 100 - spin.value(),
                                                                            "resets_at": moment + 3600}]}
        records = {account["id"]: account for account in self.accounts}
        remaining = runtime.quota_state(snapshots.get(current, {}), self.environment_id, moment, plan.interval)[0]
        if remaining is None:
            self.status.setText(tr("Preview: the current account's quota is unknown, so nothing changes."))
            return "unknown", None
        others = {key: value for key, value in snapshots.items() if key != current}

        def choose(pool, strategy):
            selection = runtime.select_quota_account(replace(plan, account_ids=pool, strategy=strategy),
                                                     records, others, self.environment_id, moment)
            return selection.account_id if selection.account_id != current else None
        facts = {"low": remaining <= plan.threshold, "exhausted": remaining <= 0,
                 "available": choose(plan.account_ids, "priority") is not None}
        trace = []
        outcome, target = flow_outcome(quota_flow(plan), facts.get, choose, trace)
        self.canvas.trace = trace
        self.canvas.update()
        text = {"handoff": tr("Preview: switch to {name} at the next safe point.", name=self.names.get(target, target)),
                "wait": tr("Preview: wait for the quota reset, then continue the task automatically."),
                "notify": tr("Preview: notify only; you continue the task by hand."),
                "keep": tr("Preview: keep the current account.")}[outcome]
        self.status.setText(text + " " + tr("Path: {path}", path=" → ".join(self.title_of(self.block(i)) for i in trace)))
        return outcome, target
