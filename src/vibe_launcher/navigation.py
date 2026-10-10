"""Keyboard focus navigation: a live, theme-coloured overlay that outlines and numbers every target."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QPlainTextEdit, QWidget
from shiboken6 import isValid

from .i18n import tr
from . import shortcuts
from .theme import ACCENT_BORDER_A, BORDER_A, BORDER_STRONG_A, C, R, S, T, font, qcolor, theme_bus

DIGITS = {getattr(Qt.Key, f"Key_{i}"): i for i in range(1, 10)}
_CAP_H, _GAP, _PAD = 20, S.xs, S.sm  # hint bar: key-cap height, spacing, bar padding
ARROWS = {Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down", Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right"}


def cycle_window(owner: QWidget, step: int) -> None:
    windows = [w for w in QApplication.topLevelWidgets() if type(w) is type(owner) and w.isVisible()]
    windows.sort(key=id)
    if len(windows) > 1 and owner in windows:
        target = windows[(windows.index(owner) + step) % len(windows)]
        target.raise_()
        target.activateWindow()
        target.setFocus(Qt.FocusReason.ShortcutFocusReason)


class _Overlay(QWidget):
    """Transparent layer over the whole window. It paints live outlines (no screenshots) and never takes input."""

    def __init__(self, navigation: "FocusNavigation", window: QWidget):
        super().__init__(window)
        self.navigation = navigation
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            self.navigation.paint(painter, self.rect())
        finally:
            painter.end()


class FocusNavigation(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.active = False
        self.target = None
        self.original = None
        self.choices: list[QWidget] = []
        self.overlay = _Overlay(self, window)
        theme_bus().changed.connect(self.overlay.update)     # a theme switch mid-navigation repaints at once
        QApplication.instance().installEventFilter(self)

    def candidates(self) -> list[QWidget]:
        workspace = self.window.workspace
        if self.window.current_page() == "workspace" and workspace.current_tabs():
            result = []
            for tab in workspace.current_tabs():
                if tab.pane.isVisible() and workspace.area.on_screen(tab.pane):
                    result.append(tab.terminal)
                    if tab.pane.composer_visible():
                        result.append(tab.pane.composer.edit)
            return result
        return [w for w in self.window.centralWidget().findChildren(QWidget)
                if isinstance(w, (QLineEdit, QPlainTextEdit, QComboBox)) and w.isVisible() and w.isEnabled()]

    def start(self) -> None:
        if self.active:
            self.stop()
            return
        choices = self.candidates()
        if not choices:
            return
        self.original = QApplication.focusWidget()
        self.active = True
        focus = self.original
        selected = next((w for w in choices if w is focus or (focus and w.isAncestorOf(focus))), choices[0])
        self.choose(selected)
        self.overlay.setGeometry(self.window.rect())
        self.overlay.show()
        self.overlay.raise_()

    def choose(self, target: QWidget) -> None:
        self.target = target
        for tab in self.window.workspace.tabs():
            if target is tab.terminal or target is tab.pane.composer.edit:
                self.window.workspace.select(tab.info.id, focus=False)
                break
        # Selecting another tab can change what is on screen, so the numbering follows the current view.
        self.choices = self.candidates()
        self.overlay.raise_()
        self.overlay.update()

    def jump(self, number: int) -> None:
        self.choices = [w for w in self.choices if isValid(w)]
        if 1 <= number <= len(self.choices):
            self.choose(self.choices[number - 1])

    def stop(self, *, restore: bool = False) -> None:
        if not self.active:
            return
        self.active = False
        self.overlay.hide()
        self.choices = []
        target = self.original if restore else self.target
        if restore and target is not None and isValid(target):
            for tab in self.window.workspace.tabs():
                if target is tab.terminal or tab.terminal.isAncestorOf(target) or target is tab.pane.composer.edit:
                    self.window.workspace.select(tab.info.id, focus=False)
                    break
        if target is not None and isValid(target) and target.isVisible():
            if hasattr(target, "focus_terminal"):
                target.focus_terminal()
            else:
                target.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.target = self.original = None

    def move(self, direction: str) -> None:
        if self.target is None or not isValid(self.target):
            self.stop()
            return
        center = self.target.mapTo(self.window, self.target.rect().center())
        dx, dy = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}[direction]
        scored = []
        for candidate in self.candidates():
            if candidate is self.target:
                continue
            point = candidate.mapTo(self.window, candidate.rect().center()) - center
            forward = point.x() * dx + point.y() * dy
            sideways = abs(point.y() * dx - point.x() * dy)
            if forward > 0:
                scored.append((forward + sideways * 3, candidate))
        if scored:
            self.choose(min(scored, key=lambda item: item[0])[1])
        elif self.window.current_page() == "workspace" and self.window.workspace.area.mode == "tabs":
            workspace = self.window.workspace
            order = [tab.info.id for tab in workspace.current_tabs()]
            index = order.index(workspace.selected_id())
            workspace.select(order[(index + (-1 if direction in {"left", "up"} else 1)) % len(order)], focus=False)
            tab = self.window.workspace.tab(self.window.workspace.selected_id())
            if tab:
                self.choose(tab.terminal)

    def switch_input(self) -> None:
        for tab in self.window.workspace.tabs():
            if self.target in (tab.terminal, tab.pane.composer.edit):
                if self.target is tab.terminal:
                    tab.pane.set_composer_visible(True)
                    self.choose(tab.pane.composer.edit)
                else:
                    self.choose(tab.terminal)
                return
        choices = self.candidates()
        if self.target in choices:
            self.choose(choices[(choices.index(self.target) + 1) % len(choices)])

    # -- drawing ---------------------------------------------------------------------------------

    def label_for(self, widget: QWidget) -> str:
        for tab in self.window.workspace.tabs():
            if widget is tab.terminal:
                return tab.info.label
            if widget is tab.pane.composer.edit:
                return tab.info.label + " · " + tr("Input bar")
        placeholder = getattr(widget, "placeholderText", None)
        return widget.accessibleName() or (placeholder() if callable(placeholder) else "") or type(widget).__name__

    def entries(self) -> list[tuple[int, QWidget, QRect, str]]:
        """(number, widget, rectangle in window coordinates, name) for every target currently drawn."""
        result = []
        for number, widget in enumerate((w for w in self.choices if isValid(w) and w.isVisible()), start=1):
            rect = QRect(widget.mapTo(self.window, QPoint()), widget.size()).intersected(self.window.rect())
            if not rect.isEmpty():
                result.append((number, widget, rect, self.label_for(widget)))
        return result

    def hint_chips(self) -> list[tuple[list[str], str]]:
        b = shortcuts.bindings()
        count = min(9, len(self.choices))
        move_keys = ["←↑↓→", "/".join(b[k] for k in ("nav.up", "nav.left", "nav.down", "nav.right"))]
        chips = [(move_keys, tr("Move"))]
        if count > 1:
            chips.append((["1–%d" % count], tr("Jump to")))
        chips += [([b["nav.input"]], tr("Terminal ↔ input")), (["Enter"], tr("Confirm")), (["Esc"], tr("Cancel"))]
        return chips

    def hint_layout(self, area: QRect):
        """Key-cap groups placed in rows (wrapping when the window is narrow) and the bar around them."""
        cap_font, text_font = font(T.micro, mono=True), font(T.meta)
        cap_metrics, text_metrics = QFontMetrics(cap_font), QFontMetrics(text_font)
        limit = max(120, area.width() - 2 * S.lg)
        rows, row, row_w = [], [], 0.0
        for keys, label in self.hint_chips():
            caps = [cap_metrics.horizontalAdvance(k) + S.sm + 2 for k in keys]
            label_w = text_metrics.horizontalAdvance(label) + 2
            width = sum(caps) + _GAP * len(caps) + label_w
            if row and row_w + S.md + width + 2 * _PAD > limit:
                rows.append((row, row_w))
                row, row_w = [], 0.0
            row_w += (S.md if row else 0) + width
            row.append((keys, caps, label, label_w))
        rows.append((row, row_w))
        width = min(limit, max(w for _, w in rows) + 2 * _PAD)
        height = len(rows) * _CAP_H + (len(rows) - 1) * _GAP + _PAD
        bar = QRectF(area.left() + (area.width() - width) / 2, area.top() + S.md, width, height)
        return bar, rows

    def paint(self, painter: QPainter, area: QRect) -> None:
        accent = C.accent
        small, label_font = font(T.micro, mono=True), font(T.micro)
        bar, rows = self.hint_layout(area)
        for number, widget, rect, name in self.entries():
            selected = widget is self.target
            box = QRectF(rect).adjusted(2, 2, -2, -2)
            if selected:
                painter.setPen(QPen(qcolor(accent), 2.5))
                painter.setBrush(qcolor(accent, 0.08))
            else:
                painter.setPen(QPen(qcolor(accent, ACCENT_BORDER_A * 0.75), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(box, R.md, R.md)
            # Number badge at the top-left (below the key bar if it would hide there), then the target's name beside it.
            painter.setFont(small)
            badge = QRectF(box.left() + S.xs + 2, box.top() + S.xs + 2, 20, 20)
            room = box.right() - badge.right() - S.md
            if badge.adjusted(0, 0, room, 0).intersects(bar) and bar.bottom() + S.xs + 20 < box.bottom():
                badge.moveTop(bar.bottom() + S.xs)
            painter.setPen(Qt.PenStyle.NoPen if selected else QPen(qcolor(accent, ACCENT_BORDER_A), 1))
            painter.setBrush(qcolor(accent) if selected else qcolor(C.overlay, 0.94))
            painter.drawRoundedRect(badge, R.sm, R.sm)
            painter.setPen(qcolor(C.accent_ink if selected else accent))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(number))
            if room > 24 and name:
                painter.setFont(label_font)
                metrics = QFontMetrics(label_font)
                text = metrics.elidedText(name, Qt.TextElideMode.ElideRight, int(room) - S.md)
                chip = QRectF(badge.right() + S.xs, badge.top(), metrics.horizontalAdvance(text) + S.md, badge.height())
                painter.setPen(QPen(qcolor(C.border, BORDER_A), 1))
                painter.setBrush(qcolor(C.overlay, 0.94))
                painter.drawRoundedRect(chip, R.sm, R.sm)
                painter.setPen(qcolor(C.text if selected else C.text_secondary))
                painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, text)
        self.paint_hint(painter, bar, rows)

    def paint_hint(self, painter: QPainter, bar: QRectF, rows) -> None:
        cap_font, text_font = font(T.micro, mono=True), font(T.meta)
        painter.setPen(QPen(qcolor(C.border, BORDER_STRONG_A), 1))
        painter.setBrush(qcolor(C.overlay, 0.96))
        painter.drawRoundedRect(bar, R.md, R.md)
        top = bar.top() + _PAD / 2
        for row, row_w in rows:
            x = bar.left() + (bar.width() - row_w) / 2
            for keys, caps, label, label_w in row:
                painter.setFont(cap_font)
                for key, cap_w in zip(keys, caps):
                    cap = QRectF(x, top, cap_w, _CAP_H)
                    painter.setPen(QPen(qcolor(C.border, BORDER_STRONG_A), 1))
                    painter.setBrush(qcolor(C.surface_raised))
                    painter.drawRoundedRect(cap, 4, 4)
                    painter.setPen(qcolor(C.text))
                    painter.drawText(cap, Qt.AlignmentFlag.AlignCenter, key)
                    x += cap_w + _GAP
                painter.setFont(text_font)
                painter.setPen(qcolor(C.text_secondary))
                painter.drawText(QRectF(x, top, label_w, _CAP_H), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
                x += label_w + S.md
            top += _CAP_H + _GAP

    # -- events ----------------------------------------------------------------------------------

    def eventFilter(self, watched, event):  # noqa: N802
        if not self.active:
            return False
        if watched is self.window:
            if event.type() in {QEvent.Type.WindowDeactivate, QEvent.Type.Close, QEvent.Type.Hide}:
                self.stop()
                return False
            if event.type() == QEvent.Type.Resize:
                self.overlay.setGeometry(self.window.rect())
                self.choices = self.candidates()
                self.overlay.raise_()
                self.overlay.update()
        if not isinstance(watched, QWidget) or watched.window() is not self.window:
            return False
        if event.type() == QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        if event.type() == QEvent.Type.KeyRelease:
            return True
        if event.type() != QEvent.Type.KeyPress:
            return False
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.stop(restore=True)
        elif key in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self.stop()
        elif shortcuts.action_for(event) == "navigate":
            self.stop()
        else:
            action = shortcuts.action_for(event, navigation=True)
            if action in {"nav.up", "nav.down", "nav.left", "nav.right"}:
                self.move(action.split(".")[1])
            elif action == "nav.input":
                self.switch_input()
            elif key in ARROWS:
                self.move(ARROWS[key])
            elif key in DIGITS:
                self.jump(DIGITS[key])
        event.accept()
        return True
