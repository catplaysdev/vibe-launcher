"""Quota plan editor: a library of saved plans, a block canvas with wiring, an options panel and a preview.

Plans are declarative data (runtime.flow_issues / flow_outcome); the editor only draws and edits that data. Every
connection can be made with the mouse (drag from a branch port, or the right-click "Connect to" menu) and with the
keyboard (block list + "Connect to" boxes in the options panel; Ctrl+arrows move a block)."""
from __future__ import annotations

import copy
import time
import uuid

from dataclasses import replace

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, QScrollArea,
                               QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from . import runtime
from .i18n import tr
from .kit import ConfirmDialog, label, make_button
from .quota_policy import TEMPLATES, Policy, PolicyStore, flow_issues, flow_outcome, quota_flow, template_plan
from .theme import C, font, qcolor

NODE_W, NODE_H, PORT = 168, 58, 7


def kind_label(kind: str) -> str:
    return {"start": tr("Quota check"), "condition": tr("Condition"), "select": tr("Select account"),
            "handoff": tr("Safe handoff"), "wait": tr("Wait and verify recovery"), "notify": tr("Notify only"),
            "keep": tr("Keep current account")}[kind]


def test_label(test: str) -> str:
    return {"low": tr("Low quota?"), "exhausted": tr("Quota exhausted?"),
            "available": tr("Another account available?")}.get(test, tr("Choose a condition"))


def strategy_label(strategy: str) -> str:
    return {"priority": tr("In order"), "round-robin": tr("Round robin"),
            "available-quota": tr("Most quota left")}.get(strategy, strategy)


def template_label(template: str) -> str:
    return {"priority": tr("Take over in order"), "round-robin": tr("Rotate accounts"),
            "available-quota": tr("Most quota first")}[template]


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
            "unguarded": tr("Switch only after a low-quota or exhausted condition answered yes")}.get(code, code)


class FlowCanvas(QWidget):
    """Draws the blocks and their wires; a block is selected, dragged or wired here."""
    selected = Signal(str)
    changed = Signal()

    def __init__(self, editor: "QuotaPolicyDialog"):
        super().__init__(editor)
        self.editor = editor
        self.current = ""
        self.trace: list = []
        self._drag = None               # ("move", id, offset) or ("wire", id, branch, point)
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
        for edge in self.editor.edges:
            source, target = nodes.get(edge["from"]), nodes.get(edge["to"])
            if not source or not target or edge["branch"] not in runtime.FLOW_BRANCHES.get(source["type"], ()):
                continue
            start = self.port(source, edge["branch"])
            end_rect = self.rect_of(target)
            end = QPointF(end_rect.left(), end_rect.center().y())
            on_trace = edge["from"] in self.trace and edge["to"] in self.trace
            painter.setPen(QPen(qcolor(C.accent if on_trace else C.text_secondary), 2.4 if on_trace else 1.6))
            path = QPainterPath(start)
            bend = max(40.0, abs(end.x() - start.x()) / 2)
            path.cubicTo(start + QPointF(bend, 0), end - QPointF(bend, 0), end)
            painter.drawPath(path)
            painter.drawLine(end, end + QPointF(-7, -5))
            painter.drawLine(end, end + QPointF(-7, 5))
            text = branch_label(edge["branch"])
            if text:
                painter.setPen(qcolor(C.text_secondary))
                first = runtime.FLOW_BRANCHES[source["type"]].index(edge["branch"]) == 0
                painter.drawText(start + QPointF(10, -6 if first else 16), text)      # above / below its port
        if self._drag and self._drag[0] == "wire":
            painter.setPen(QPen(qcolor(C.accent), 2, Qt.PenStyle.DashLine))
            painter.drawLine(self.port(nodes[self._drag[1]], self._drag[2]), self._drag[3])
        problems = self.editor.node_issues
        for node in self.editor.nodes:
            rect = self.rect_of(node)
            hue = {"start": C.info, "condition": C.warning, "select": C.accent, "handoff": C.success,
                   "wait": C.info, "notify": C.text_secondary, "keep": C.text_secondary}[node["type"]]
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(C.surface_raised))
            painter.drawRoundedRect(rect, 8, 8)
            painter.setBrush(qcolor(hue))
            painter.drawRoundedRect(QRectF(rect.left(), rect.top(), 5, rect.height()), 2, 2)
            border = C.danger if node["id"] in problems else (C.accent if node["id"] == self.current else C.border)
            width = 2.4 if node["id"] in problems or node["id"] == self.current else 1.0
            painter.setPen(QPen(qcolor(border, 1.0 if width > 1 else 0.35), width))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, 8, 8)
            painter.setPen(qcolor(C.text))
            painter.setFont(font(12, QFont.Weight.DemiBold))
            painter.drawText(rect.adjusted(14, 6, -8, -NODE_H / 2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             self.editor.title_of(node))
            painter.setFont(font(11))
            painter.setPen(qcolor(C.text_secondary))
            painter.drawText(rect.adjusted(14, NODE_H / 2, -8, -6), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(self.editor.detail_of(node), Qt.TextElideMode.ElideRight,
                                                              int(rect.width()) - 22))
            if node["id"] in problems:
                painter.setPen(qcolor(C.danger))
                painter.setFont(font(13, QFont.Weight.Bold))
                painter.drawText(QRectF(rect.right() - 22, rect.top() + 2, 18, 18), Qt.AlignmentFlag.AlignCenter, "!")
            for branch in runtime.FLOW_BRANCHES.get(node["type"], ()):
                point = self.port(node, branch)
                connected = any(e["from"] == node["id"] and e["branch"] == branch for e in self.editor.edges)
                painter.setPen(QPen(qcolor(C.text_secondary), 1.4))
                painter.setBrush(qcolor(C.accent) if connected else qcolor(C.surface))
                painter.drawEllipse(point, PORT / 2 + 1, PORT / 2 + 1)
        if self.hasFocus():
            painter.setPen(QPen(qcolor(C.accent, 0.6), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(self.rect()).adjusted(1, 1, -1, -1))

    # mouse
    def mousePressEvent(self, event):  # noqa: N802
        point = event.position()
        hit = self.port_at(point)
        if hit and event.button() == Qt.MouseButton.LeftButton:
            self._drag = ("wire", hit[0]["id"], hit[1], point)
            return
        node = self.node_at(point)
        if node:
            self.editor.select_block(node["id"])
            if event.button() == Qt.MouseButton.LeftButton:
                self._drag = ("move", node["id"], point - QPointF(node.get("x", 0), node.get("y", 0)))
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
            target = self.node_at(event.position())
            if target and target["id"] != drag[1]:
                self.editor.connect_branch(drag[1], drag[2], target["id"])
        elif drag:
            self.changed.emit()
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
    """Saved plans of one environment and CLI. ``result_plan`` is the plan to apply when the dialog is accepted."""
    def __init__(self, parent=None, accounts=None, environment_id="local", tool="codex", store: PolicyStore | None = None,
                 current_plan_id: str = "", snapshots=None):
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
        self._dirty = False
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
        self.on_gaps = QComboBox()
        self.on_gaps.addItem(tr("Stop and show what is missing"), "block")
        self.on_gaps.addItem(tr("Switch without them and notify"), "allow")
        self.on_gaps.setAccessibleName(tr("When the target account lacks plugins, apps or MCP sign-ins"))
        row = QHBoxLayout()
        row.addWidget(self.threshold)
        row.addWidget(label(tr("check every")))
        row.addWidget(self.interval)
        header.addRow(tr("Plan name"), self.name)
        header.addRow(tr("Low quota at or below"), row)
        header.addRow(tr("Missing plugins, apps or MCP sign-ins"), self.on_gaps)
        middle.addLayout(header)
        palette = QHBoxLayout()
        self.add_buttons = {}
        for kind, text in (("condition", tr("Add condition")), ("select", tr("Add account selection")),
                           ("wait", tr("Add wait")), ("notify", tr("Add notification")), ("keep", tr("Add keep"))):
            button = make_button(text, "ghost", small=True, callback=lambda checked=False, kind=kind: self.add_block(kind))
            self.add_buttons[kind] = button
            palette.addWidget(button)
        palette.addStretch(1)
        middle.addLayout(palette)
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
        right.addWidget(holder, 2)
        self.delete_block_button = make_button(tr("Delete block"), "ghost", small=True, callback=self.delete_block)
        right.addWidget(self.delete_block_button)
        panel = QWidget()
        panel.setLayout(right)
        panel.setFixedWidth(280)
        outer.addWidget(panel)
        for widget in (self.name, self.on_gaps):
            (widget.textEdited if isinstance(widget, QLineEdit) else widget.currentIndexChanged).connect(self._changed)
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
        self.on_gaps.setCurrentIndex(max(0, self.on_gaps.findData(plan.on_gaps)))
        flow = copy.deepcopy(quota_flow(plan))
        self.nodes, self.edges = flow["nodes"], flow["edges"]
        self._loading = False
        self._dirty = plan.id not in self.plans
        self.canvas.trace = []
        self._refresh(select=self.nodes[0]["id"] if self.nodes else "")

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
        """The edited plan; raises ValueError (with what to fix) when it cannot run."""
        if self.issues:
            raise ValueError(issue_text(self.issues[0][1]))
        pools = [item for node in self.nodes if node["type"] == "select" for item in node["accounts"]]
        watched = list(dict.fromkeys(pools)) or list(self.names)
        old = self.plans.get(self.plan_id)
        return runtime.checked_quota_policy({
            "id": self.plan_id, "plan_id": self.plan_id, "name": self.name.text().strip() or tr("Quota plan"),
            "environment_id": self.environment_id, "account_ids": watched, "threshold": self.threshold.value(),
            "interval": self.interval.value(), "on_gaps": self.on_gaps.currentData(),
            "revision": (old.revision + (1 if self._dirty else 0)) if old else 1,
            "flow": {"nodes": copy.deepcopy(self.nodes), "edges": copy.deepcopy(self.edges)}})

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
        try:
            plan = self.current_plan()
        except ValueError as error:
            self.status.setText(str(error))
            return False
        if not self._store_plan(plan):
            return False
        self._dirty = False
        self.reload(plan.id)
        self.status.setText(tr("Plan saved. Terminals that use it change only when you apply it again."))
        return True

    def save_as(self):
        self.plan_id, self._dirty = uuid.uuid4().hex, True
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
        return {"start": tr("Runs at every quota check"), "condition": kind_label("condition"),
                "handoff": tr("Required; waits for a verified safe point"),
                "wait": tr("Resumes only after a verified reset"), "notify": tr("You continue by hand"),
                "keep": tr("Nothing changes")}[node["type"]]

    def add_block(self, kind: str):
        ident = kind + "-" + uuid.uuid4().hex[:6]
        node = {"id": ident, "type": kind, "x": 40 + 24 * (len(self.nodes) % 8), "y": 260 + 12 * (len(self.nodes) % 8)}
        if kind == "condition":
            node["test"] = "low"
        if kind == "select":
            node.update(accounts=list(self.names), strategy="priority")
        self.nodes.append(node)
        self._changed()
        self.select_block(ident)

    def delete_block(self):
        node = self.block(self.canvas.current)
        if not node or node["type"] in ("start", "handoff"):
            self.status.setText(tr("The quota check and the safe handoff are part of every plan"))
            return
        self.nodes.remove(node)
        self.edges = [edge for edge in self.edges if node["id"] not in (edge["from"], edge["to"])]
        self._changed()
        self.select_block(self.nodes[0]["id"] if self.nodes else "")

    def connect_branch(self, source: str, branch: str, target: str | None):
        self.edges = [edge for edge in self.edges if not (edge["from"] == source and edge["branch"] == branch)]
        if target:
            self.edges.append({"from": source, "branch": branch, "to": target})
        self._changed()

    def connect_menu(self, node: dict) -> QMenu:
        menu = QMenu(self)
        for branch in runtime.FLOW_BRANCHES.get(node["type"], ()):
            sub = menu.addMenu(tr("Connect {branch} to", branch=branch_label(branch) or tr("next")))
            for other in self.nodes:
                if other["id"] != node["id"] and other["type"] != "start":
                    sub.addAction(self.title_of(other) + " · " + other["id"],
                                  lambda branch=branch, other=other["id"]: self.connect_branch(node["id"], branch, other))
        if node["type"] not in ("start", "handoff"):
            menu.addSeparator()
            menu.addAction(tr("Delete block"), self.delete_block)
        return menu

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
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    if child.widget():
                        child.widget().deleteLater()
        node = self.block(self.canvas.current)
        self.connect_boxes = {}
        self.delete_block_button.setEnabled(bool(node) and node["type"] not in ("start", "handoff"))
        if not node:
            return
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.options.addLayout(form)
        form.addRow(label(kind_label(node["type"]), "heading"))
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
            pool.setMaximumHeight(140)

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
        hint = label(self.detail_of(node) if node["type"] != "select" else tr("Healthy conversations keep their account."), wrap=True)
        form.addRow(hint)
        for branch in runtime.FLOW_BRANCHES.get(node["type"], ()):
            box = QComboBox()
            box.setAccessibleName(tr("Connect {branch} to", branch=branch_label(branch) or tr("next")))
            box.addItem(tr("Not connected"), "")
            for other in self.nodes:
                if other["id"] != node["id"] and other["type"] != "start":
                    box.addItem(self.title_of(other) + " · " + other["id"], other["id"])
            current = next((e["to"] for e in self.edges if e["from"] == node["id"] and e["branch"] == branch), "")
            box.setCurrentIndex(max(0, box.findData(current)))
            self.connect_boxes[branch] = box
            box.currentIndexChanged.connect(
                lambda _i, branch=branch, box=box, node=node: self.connect_branch(node["id"], branch, box.currentData() or None))
            form.addRow(tr("Connect {branch} to", branch=branch_label(branch) or tr("next")), box)
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
        self._dirty = True
        self.canvas.trace = []
        self._refresh(select=self.canvas.current)

    def _refresh(self, select: str = ""):
        self.issues = flow_issues({"nodes": self.nodes, "edges": self.edges}, tuple(self.names))
        self.node_issues = {}
        for ident, code in self.issues:
            self.node_issues.setdefault(ident, []).append(code)
        self._loading = True
        self.block_list.clear()
        for node in self.nodes:
            item = QListWidgetItem(("⚠ " if node["id"] in self.node_issues else "") + self.title_of(node) + " · " + node["id"])
            item.setData(Qt.ItemDataRole.UserRole, node["id"])
            self.block_list.addItem(item)
        self._loading = False
        if self.issues:
            self.status.setText(tr("Fix before saving:") + " " + "; ".join(dict.fromkeys(issue_text(code) for _, code in self.issues)))
        else:
            self.status.setText(tr("The plan is valid. Preview it with made-up quota; no work starts."))
        self.select_block(select if self.block(select) else (self.nodes[0]["id"] if self.nodes else ""))
        self.canvas.updateGeometry()
        self.canvas.adjustSize()
        self._update_buttons()

    def _update_buttons(self):
        valid = not self.issues and bool(self.nodes)
        for button in (self.save_button, self.apply_button, self.save_as_button, self.preview_button):
            button.setEnabled(valid)
        self.delete_button.setEnabled(self.plan_id in self.plans)
        self.rename_button.setEnabled(bool(self.nodes))

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
                "wait": tr("Preview: no account is available; wait and verify the recovery."),
                "notify": tr("Preview: no account is available; notify and continue by hand."),
                "keep": tr("Preview: keep the current account.")}[outcome]
        self.status.setText(text + " " + tr("Path: {path}", path=" → ".join(self.title_of(self.block(i)) for i in trace)))
        return outcome, target
