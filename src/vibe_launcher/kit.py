"""Reusable, token-driven UI components for Vibe Launcher.

Nothing in here knows about environments, SSH or the store. Components paint
themselves from ``theme`` tokens so the whole app stays visually consistent.
"""

from __future__ import annotations

from .i18n import tr

from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import (
    Property, QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer,
    QVariantAnimation, Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QIcon, QKeyEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QApplication, QDialog, QFrame, QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect, QGridLayout, QHBoxLayout, QLabel, QLayout, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QPushButton, QSizePolicy, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QVBoxLayout, QWidget,
)

from . import icons
from .theme import (
    ACCENT_BORDER_A, ACCENT_SUBTLE_A, BORDER_STRONG_A, C, M, R, S, Size, T, TINT_A, TINT_STRONG_A, TONES, families as theme_families, font, qcolor, resolve,
    section_hue,
)

QWIDGETSIZE_MAX = 16777215
KEYBOARD_REASONS = (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason, Qt.FocusReason.ShortcutFocusReason)


def dispose_widget(widget: QWidget) -> None:
    """Hide a widget and let Qt delete it on the next loop turn.

    Never ``setParent(None)`` before ``deleteLater()``: that hands ownership to Python, and the wrapper's garbage
    collection then frees the same C++ object a second time (heap corruption that crashes somewhere unrelated).
    Hiding is enough to take it off screen, and deleting later is safe even from inside the widget's own signal.
    """
    widget.setProperty("disposed", True)
    widget.hide()
    widget.deleteLater()


def later(owner: QObject, msec: int, callback: Callable[[], Any]) -> QTimer:
    """Run ``callback`` after ``msec`` unless ``owner`` is gone by then.

    ``QTimer.singleShot(ms, bound_method)`` still fires after the widget it belongs to was destroyed (closing a window,
    removing a tab) and then touches freed Qt objects. A timer parented to ``owner`` dies with it.
    """
    timer = QTimer(owner)
    timer.setSingleShot(True)
    timer.timeout.connect(callback)
    timer.timeout.connect(timer.deleteLater)
    timer.start(msec)
    return timer


def lerp_color(a: str, b: str, t: float, alpha: float = 1.0) -> QColor:
    ca, cb = QColor(a), QColor(b)
    c = QColor(round(ca.red() + (cb.red() - ca.red()) * t), round(ca.green() + (cb.green() - ca.green()) * t),
               round(ca.blue() + (cb.blue() - ca.blue()) * t))
    c.setAlphaF(alpha)
    return c


# --------------------------------------------------------------------------- mixins


class KbFocus:
    """Remember whether focus arrived via the keyboard so widgets show a ring only then."""

    _kb = False

    def focusInEvent(self, event):  # noqa: N802 (Qt API)
        self._kb = event.reason() in KEYBOARD_REASONS
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):  # noqa: N802
        self._kb = False
        super().focusOutEvent(event)
        self.update()


class Hover:
    """Animated 0..1 hover amount in ``self._hv`` (150ms, no jumps)."""

    _hv = 0.0

    def _init_hover(self) -> None:
        self._hv_anim = QVariantAnimation(self)
        self._hv_anim.setDuration(M.hover)
        self._hv_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hv_anim.valueChanged.connect(self._set_hover)

    def _set_hover(self, value) -> None:
        self._hv = float(value)
        self.update()

    def _hover_to(self, target: float) -> None:
        self._hv_anim.stop()
        self._hv_anim.setStartValue(self._hv)
        self._hv_anim.setEndValue(target)
        self._hv_anim.start()

    def enterEvent(self, event):  # noqa: N802
        self._hover_to(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self._hover_to(0.0)
        super().leaveEvent(event)


def draw_focus_ring(painter: QPainter, rect: QRectF, radius: float) -> None:
    painter.save()
    pen = QPen(qcolor(C.accent, 0.85), 2)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), radius, radius)
    painter.restore()


def draw_icon(painter: QPainter, widget: QWidget, name: str, color: str | QColor, x: float, y: float, size: int) -> None:
    painter.drawPixmap(round(x), round(y), icons.pixmap(name, resolve(color), size, max(2.0, widget.devicePixelRatioF())))


# --------------------------------------------------------------------------- layouts & labels


class FlowLayout(QLayout):
    """Wrapping layout (chips). Reports height-for-width so cards can size themselves."""

    def __init__(self, parent: QWidget | None = None, h_spacing: int = S.sm - 2, v_spacing: int = S.sm - 2):
        super().__init__(parent)
        self._items: list[Any] = []
        self._h, self._v = h_spacing, v_spacing

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def clear(self) -> None:
        while self._items:
            item = self._items.pop()
            widget = item.widget()
            if widget is not None:
                dispose_widget(widget)

    def _layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_height = area.x(), area.y(), 0
        for item in self._items:
            if item.widget() is not None and item.widget().isHidden():
                continue
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h
            if next_x - self._h > area.right() + 1 and line_height > 0:
                x = area.x()
                y += line_height + self._v
                next_x = x + hint.width() + self._h
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + m.bottom()


class ElidedLabel(QLabel):
    """Single-line label that elides instead of forcing the layout wider (long names/paths)."""

    def __init__(self, text: str = "", *, px: int = T.body, weight=None, color: str = "text", mono: bool = False,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._full = text
        self._color = color
        self.setFont(font(px, weight if weight is not None else QFont.Weight.Normal, mono))
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        QLabel.setText(self, text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text
        QLabel.setText(self, text)
        self.updateGeometry()
        self._sync_tip()
        self.update()

    def setColor(self, color: str) -> None:  # noqa: N802
        self._color = color
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = QFontMetrics(self.font())
        return QSize(fm.horizontalAdvance(self._full) + 2, fm.height() + 4)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(24, QFontMetrics(self.font()).height() + 4)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_tip()

    def _sync_tip(self) -> None:
        fm = QFontMetrics(self.font())
        self.setToolTip(self._full if fm.horizontalAdvance(self._full) > self.width() and self.width() > 0 else "")

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setFont(self.font())
        p.setPen(QColor(resolve(self._color)))
        text = QFontMetrics(self.font()).elidedText(self._full, Qt.TextElideMode.ElideRight, self.width())
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)


def set_tone(widget: QWidget, tone: str) -> None:
    """Colour a label by status tone (ok / warn / danger / info / accent / muted; "" for the default)."""
    if widget.property("tone") != tone:
        widget.setProperty("tone", tone)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def label(text: str = "", role: str = "", *, wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if role:
        lab.setProperty("role", role)
    lab.setWordWrap(wrap)
    if wrap:
        lab.setMinimumWidth(0)
        lab.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return lab


# --------------------------------------------------------------------------- buttons


def make_button(text: str = "", variant: str = "secondary", icon_name: str | None = None, *, small: bool = False,
                tip: str = "", callback: Callable[..., Any] | None = None, tint: str = "") -> QPushButton:
    """Button factory. Hierarchy: primary (one per view) > secondary > ghost; danger is reserved.
    ``tint`` (a colour token) colours the icon only, e.g. one hue per service."""
    button = QPushButton(text)
    button.setProperty("variant", variant)
    if small:
        button.setProperty("size", "sm")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon_name:
        button.setProperty("vlIcon", icon_name)
        if tint:
            button.setProperty("vlTint", tint)
        _tint_button(button)
    if tip:
        button.setToolTip(tip)
        button.setAccessibleDescription(tip)
    if callback is not None:
        button.clicked.connect(callback)
    return button


def _tint_button(button: QPushButton) -> None:
    """(Re)draw a ``make_button`` icon in the active theme's colours."""
    name, variant = button.property("vlIcon"), button.property("variant")
    color = resolve(button.property("vlTint")) if button.property("vlTint") else         {"primary": C.accent_ink, "danger": C.danger, "ghost": C.text_secondary}.get(variant, C.text)
    size = 14 if button.property("size") == "sm" else 16
    ic = QIcon()
    ic.addPixmap(icons.pixmap(name, color, size, 2.0), QIcon.Mode.Normal)
    ic.addPixmap(icons.pixmap(name, C.text_disabled, size, 2.0), QIcon.Mode.Disabled)
    button.setIcon(ic)
    button.setIconSize(QSize(size, size))


def glyph_label(name: str, color: str = "text_muted", size: int = 16) -> QLabel:
    """A small icon as a label, in a theme colour token; follows a theme switch (see ``retheme_widgets``)."""
    glyph = QLabel()
    glyph.setProperty("vlGlyph", f"{name}|{color}|{size}")
    _tint_glyph(glyph)
    return glyph


def _tint_glyph(glyph: QLabel) -> None:
    name, color, size = str(glyph.property("vlGlyph")).split("|")
    glyph.setPixmap(icons.pixmap(name, resolve(color), int(size), 2.0))


def apply_fonts(previous: tuple[list[str], list[str]]) -> None:
    """After the interface / terminal font changed: give the widgets that used the old stacks the new ones.

    ``previous`` is ``(theme.families(), theme.families(mono=True))`` from before the change; sizes and weights stay."""
    app = QApplication.instance()
    if app is None:
        return
    new_ui, new_mono = theme_families(), theme_families(mono=True)
    base = QFont(app.font())
    base.setFamilies(new_ui)
    app.setFont(base)
    for widget in QApplication.allWidgets():
        current = widget.font().families()
        target = new_ui if current == previous[0] else new_mono if current == previous[1] else None
        if target is not None and target != current:
            f = QFont(widget.font())
            f.setFamilies(target)
            widget.setFont(f)


def retheme_widgets() -> None:
    """After a theme switch: re-tint baked icons and repaint everything (painted widgets read tokens when painting).

    Widgets with colours of their own can define ``retheme()``; it is called here.
    """
    for widget in QApplication.allWidgets():
        if isinstance(widget, QPushButton) and widget.property("vlIcon"):
            _tint_button(widget)
        elif isinstance(widget, QLabel) and widget.property("vlGlyph"):
            _tint_glyph(widget)
        retheme = getattr(widget, "retheme", None)
        if callable(retheme):
            retheme()
        widget.update()


class IconButton(KbFocus, Hover, QAbstractButton):
    """Quiet icon-only button (overflow menu, pin, close...)."""

    def __init__(self, name: str, tip: str = "", *, size: int = 32, icon_size: int = 16, tone: str = "normal",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._init_hover()
        self.icon_name, self._icon_size, self._tone = name, icon_size, tone
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(tip)
        self.setAccessibleName(tip or name)
        self.active_color: str | None = None   # e.g. warning-yellow when pinned

    def set_icon(self, name: str) -> None:
        self.icon_name = name
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        base = resolve(self.active_color) or (C.danger if self._tone == "danger" else C.text_secondary)
        hover_fg = resolve(self.active_color) or (C.danger if self._tone == "danger" else C.text)
        if not self.isEnabled():
            color = QColor(C.text_disabled)
        else:
            color = lerp_color(base, hover_fg, self._hv)
        if self.isEnabled() and (self._hv > 0 or self.isDown()):
            tint = C.danger if self._tone == "danger" else C.border
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(tint, (0.14 if self._tone == "danger" else 0.12) * (1.0 if self.isDown() else self._hv)))
            p.drawRoundedRect(r, R.md, R.md)
        s = self._icon_size
        draw_icon(p, self, self.icon_name, color, (r.width() - s) / 2, (r.height() - s) / 2, s)
        if self._kb:
            draw_focus_ring(p, r, R.md)


# --------------------------------------------------------------------------- status badge


class StatusBadge(QWidget):
    """Pill with icon + text. Colour is semantic only, and the icon/text always carry the state too."""

    def __init__(self, text: str = "", tone: str = "unknown", icon: str = "", tip: str = "", *, max_width: int = 240,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._text, self._tone, self._icon = text, tone, icon
        self._spin = False
        self._angle = 0
        self._timer: QTimer | None = None
        self.setFixedHeight(24)
        self.setMaximumWidth(max_width)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setFont(font(T.meta, QFont.Weight.Medium))
        self.set_state(text, tone, icon, tip)

    def set_state(self, text: str, tone: str, icon: str = "", tip: str = "", spinning: bool = False) -> None:
        self._text, self._tone, self._icon = text, tone, icon
        self.setToolTip(tip)
        self.setAccessibleName(text)
        self.setAccessibleDescription(tip)
        self._set_spinning(spinning)
        self.updateGeometry()
        self.update()

    def _set_spinning(self, on: bool) -> None:
        self._spin = on
        if on and self._timer is None:
            self._timer = QTimer(self)
            self._timer.setInterval(40)
            self._timer.timeout.connect(self._tick)
        if self._timer is not None:
            self._timer.start() if on else self._timer.stop()

    def _tick(self) -> None:
        self._angle = (self._angle + 24) % 360
        self.update()

    def tone(self) -> str:
        return self._tone

    def text(self) -> str:
        return self._text

    def _has_icon(self) -> bool:
        return bool(self._icon) or self._spin

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = QFontMetrics(self.font())
        width = 9 + (12 + 5 if self._has_icon() else 0) + fm.horizontalAdvance(self._text) + 14
        return QSize(min(width, self.maximumWidth()), 24)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(28, 24)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        fg = TONES.get(self._tone, C.neutral)
        text_color = C.text_secondary if self._tone in {"unknown", "neutral"} else fg
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcolor(C.border if self._tone in {"unknown", "neutral"} else fg, 0.12))
        r = QRectF(self.rect())
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        x = 9.0
        if self._has_icon():
            cy = r.height() / 2
            if self._spin:
                pen = QPen(QColor(text_color), 1.75)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawArc(QRectF(x, cy - 5.5, 11, 11), -self._angle * 16, 270 * 16)
            elif self._icon == "dot":
                p.setBrush(QColor(fg))
                p.drawEllipse(QRectF(x + 2, cy - 3, 6, 6))
            else:
                draw_icon(p, self, self._icon, fg if self._tone not in {"unknown", "neutral"} else C.text_muted, x, cy - 6, 12)
            x += 12 + 5
        p.setPen(QColor(text_color))
        p.setFont(self.font())
        fm = QFontMetrics(self.font())
        avail = int(r.width() - x - 10)
        # Elide only when the text genuinely does not fit; fractional-DPI metrics can be off by a pixel or two.
        text = self._text if fm.horizontalAdvance(self._text) <= avail + 4 else fm.elidedText(self._text, Qt.TextElideMode.ElideRight, avail)
        p.drawText(QRectF(x, 0, r.width() - x - 4, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)


# --------------------------------------------------------------------------- sidebar


class NavButton(KbFocus, Hover, QAbstractButton):
    """Sidebar entry. The icon never moves when the sidebar collapses; only the label disappears."""

    def __init__(self, key: str, text: str, icon_name: str, *, hint: str = "", checkable: bool = True,
                 muted: bool = False, hue: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._init_hover()
        self.key, self._label, self._icon, self._hint, self._muted = key, text, icon_name, hint, muted
        self._hue = hue            # token name of this entry's colour ("" = no tile: utility buttons stay quiet)
        self._rail = False
        self.setCheckable(checkable)
        self.setFixedHeight(Size.hit)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(text)
        self.setText(text)
        self.setFont(font(T.body, QFont.Weight.Medium))

    def set_rail(self, rail: bool) -> None:
        self._rail = rail
        self.setToolTip((self._label + (f"   {self._hint}" if self._hint else "")) if rail else "")
        self.update()

    def set_icon(self, name: str, label: str | None = None) -> None:
        self._icon = name
        if label is not None:
            self._label = label
            self.setText(label)
            self.setAccessibleName(label)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(200, Size.hit)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        checked = self.isChecked()
        hue = resolve(self._hue) if self._hue else C.accent
        p.setPen(Qt.PenStyle.NoPen)
        if checked:
            p.setBrush(qcolor(hue, ACCENT_SUBTLE_A))
            p.drawRoundedRect(r, R.md, R.md)
            p.setBrush(qcolor(hue))
            p.drawRoundedRect(QRectF(0, r.height() / 2 - 9, 3, 18), 1.5, 1.5)
        elif self._hv > 0 or self.isDown():
            p.setBrush(qcolor(C.border, 0.10 * (1.0 if self.isDown() else self._hv)))
            p.drawRoundedRect(r, R.md, R.md)
        resting = C.text_muted if self._muted else C.text_secondary
        fg = QColor(C.text) if checked else lerp_color(resting, C.text, self._hv)
        text_x = 44
        if self._hue:                                    # a calm tinted tile carries the entry's colour
            tile = QRectF((r.width() - 28) / 2 if self._rail else 9, (r.height() - 28) / 2, 28, 28)   # centred when only icons show
            p.setBrush(qcolor(hue, TINT_STRONG_A if checked else TINT_A + 0.04 * self._hv))
            p.drawRoundedRect(tile, R.sm + 2, R.sm + 2)
            icon_color = QColor(hue) if checked else lerp_color(hue, C.text_secondary, 0.4)
            draw_icon(p, self, self._icon, icon_color, tile.left() + 5, tile.top() + 5, 18)
            text_x = 50
        else:
            icon_color = QColor(C.accent) if checked else fg
            # Same centre as the tinted tiles above: middle of the rail, or the tile's centre (x=23) when expanded.
            draw_icon(p, self, self._icon, icon_color, (r.width() - 20) / 2 if self._rail else 13, (r.height() - 20) / 2, 20)
        if not self._rail:
            p.setFont(self.font())
            p.setPen(fg)
            text = QFontMetrics(self.font()).elidedText(self._label, Qt.TextElideMode.ElideRight, int(r.width() - text_x - 8))
            p.drawText(QRectF(text_x, 0, r.width() - text_x - 8, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        if self._kb:
            draw_focus_ring(p, r, R.md)


class BrandMark(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(32, 32)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # The cat-and-robots logo: charcoal lines on its cream plate, the robots' eyes in the theme accent.
        p.drawPixmap(0, 0, icons.logo_pixmap(32, icons.LOGO_LINE, C.accent, plate=icons.LOGO_PLATE,
                                             dpr=max(2.0, self.devicePixelRatioF())))


class _BrandRow(KbFocus, Hover, QAbstractButton):
    """The logo and name at the top of the sidebar; a button that opens Home."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._init_hover()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName(tr("Home"))
        self.setToolTip(tr("Home"))
        row = QHBoxLayout(self)
        row.setContentsMargins(S.xs, S.xs, S.xs, S.xs)
        row.setSpacing(S.md)
        self.mark = BrandMark()
        self.mark.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.mark)
        self.label = ElidedLabel("Vibe Launcher", px=T.section, weight=QFont.Weight.DemiBold)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.label, 1)
        self.current = False

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(160, 40)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        wash = TINT_STRONG_A if self.current else 0.08 * self._hv
        if wash:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(C.accent if self.current else C.border, wash))
            p.drawRoundedRect(QRectF(self.rect()), R.md, R.md)
        if self._kb:
            draw_focus_ring(p, QRectF(self.rect()), R.md)


class Sidebar(QFrame):
    """Navigation rail: 232px expanded, 64px collapsed, animated; state is owned by the window."""

    navigate = Signal(str)
    import_requested = Signal()
    export_requested = Signal()
    appearance_requested = Signal()
    home_requested = Signal()
    collapse_toggled = Signal(bool)

    def __init__(self, items: list[tuple[str, str, str]], parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._collapsed = False
        self._forced_rail = False
        self._expanded_width = Size.sidebar_expanded
        self.setFixedWidth(self._expanded_width)
        self._anim = QPropertyAnimation(self, b"sidebarWidth", self)
        self._anim.setDuration(M.sidebar)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._sync_labels)

        root = QVBoxLayout(self)
        root.setContentsMargins(S.md, S.lg, S.md, S.md)
        root.setSpacing(S.xs)
        self.brand = _BrandRow()
        self.brand.clicked.connect(self.home_requested)
        self.brand_label = self.brand.label
        root.addWidget(self.brand)
        root.addSpacing(S.xl)

        self.buttons: dict[str, NavButton] = {}
        for index, (key, text, icon_name) in enumerate(items, start=1):
            button = NavButton(key, text, icon_name, hint=f"Ctrl+{index}", hue=section_hue(key))
            button.clicked.connect(lambda _=False, k=key: self.navigate.emit(k))
            root.addWidget(button)
            self.buttons[key] = button
        root.addStretch(1)

        self.import_button = NavButton("import", tr('kit.f5475897e4a5'), "download", checkable=False, muted=True)
        self.export_button = NavButton("export", tr('kit.9263e6bbcdf7'), "upload", checkable=False, muted=True)
        self.import_button.clicked.connect(self.import_requested)
        self.export_button.clicked.connect(self.export_requested)
        self.appearance_button = NavButton("appearance", tr("Settings"), "settings", checkable=False, muted=True)
        self.appearance_button.clicked.connect(self.appearance_requested)
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setObjectName("divider")
        self.collapse_button = NavButton("collapse", tr('kit.9188e88d9fc5'), "panel-left", hint="Ctrl+B", checkable=False, muted=True)
        self.collapse_button.clicked.connect(lambda: self.set_collapsed(not self._collapsed))
        root.addWidget(self.import_button)
        root.addWidget(self.export_button)
        root.addWidget(self.appearance_button)
        root.addSpacing(S.xs)
        root.addWidget(divider)
        root.addSpacing(S.xs)
        root.addWidget(self.collapse_button)
        self._apply_rail(False)

    # width as a Qt property so QPropertyAnimation can drive it
    def _get_width(self) -> int:
        return self.width()

    def _set_width(self, value: int) -> None:
        self.setFixedWidth(int(value))

    sidebarWidth = Property(int, _get_width, _set_width)

    def is_rail(self) -> bool:
        return self._collapsed or self._forced_rail

    def is_collapsed(self) -> bool:
        return self._collapsed

    def target_width(self) -> int:
        return Size.sidebar_rail if self.is_rail() else self._expanded_width

    def set_expanded_width(self, width: int, animate: bool = False) -> None:
        self._expanded_width = width
        self._retarget(animate)

    def set_collapsed(self, collapsed: bool, animate: bool = True, *, emit: bool = True) -> None:
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self._retarget(animate)
        if emit:
            self.collapse_toggled.emit(collapsed)

    def set_forced_rail(self, forced: bool, animate: bool = False) -> None:
        if forced == self._forced_rail:
            return
        self._forced_rail = forced
        self._retarget(animate)

    def set_current(self, key: str) -> None:
        for name, button in self.buttons.items():
            button.setChecked(name == key)
        self.brand.current = key == "home"
        self.brand.update()

    def _retarget(self, animate: bool) -> None:
        self._apply_rail(animate)
        target = self.target_width()
        self._anim.stop()
        if animate and self.isVisible() and self.width() != target:
            self._anim.setStartValue(self.width())
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self.setFixedWidth(target)
            self._sync_labels()

    def _apply_rail(self, animate: bool) -> None:
        from .shortcuts import bindings
        keys = bindings()
        for index, button in enumerate(self.buttons.values(), 1):
            button._hint = keys[f"page:{index}"]
        self.appearance_button._hint = keys["settings"]
        self.collapse_button._hint = keys["sidebar"]
        rail = self.is_rail()
        for button in (*self.buttons.values(), self.import_button, self.export_button, self.appearance_button,
                       self.collapse_button):
            button.set_rail(rail)
        self.collapse_button.set_icon("panel-left", tr('kit.879a1488a933') if self._collapsed else tr('kit.9188e88d9fc5'))
        self.collapse_button.setVisible(not self._forced_rail)
        if not rail:
            self.brand_label.show()

    def _sync_labels(self) -> None:
        self.brand_label.setVisible(not self.is_rail())


# --------------------------------------------------------------------------- page header


class PageHeader(QWidget):
    """Title + subtitle on the left, actions on the right; actions drop below when narrow."""

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None, *, icon: str = "", hue: str = "accent"):
        super().__init__(parent)
        self.title = QLabel(title)
        self.title.setProperty("role", "title")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setProperty("role", "muted")
        self.subtitle.setWordWrap(True)
        self.subtitle.setMinimumWidth(0)
        self.text_block = QWidget()
        outer = QHBoxLayout(self.text_block)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(S.md + 2)
        self.tile: _IconTile | None = None
        if icon:
            self.tile = _IconTile(44, 22, radius=R.lg)
            self.tile.set_icon(icon, hue)
            outer.addWidget(self.tile, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(S.xs)
        text.addWidget(self.title)
        text.addWidget(self.subtitle)
        outer.addLayout(text, 1)
        self.actions = QWidget()
        self.actions_layout = QHBoxLayout(self.actions)
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.actions_layout.setSpacing(S.sm)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(S.lg)
        self._grid.setVerticalSpacing(S.md)
        self._stacked: bool | None = None
        self._relayout(False)

    def add_action(self, widget: QWidget, stretch: int = 0) -> None:
        self.actions_layout.addWidget(widget, stretch)

    def _relayout(self, stacked: bool) -> None:
        if stacked == self._stacked:
            return
        self._stacked = stacked
        self._grid.removeWidget(self.text_block)
        self._grid.removeWidget(self.actions)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(1, 0)
        if stacked:
            self._grid.addWidget(self.text_block, 0, 0, 1, 2)
            self._grid.addWidget(self.actions, 1, 0, 1, 2)
        else:
            self._grid.addWidget(self.text_block, 0, 0, Qt.AlignmentFlag.AlignVCenter)
            self._grid.addWidget(self.actions, 0, 1, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout(self.width() < 780)


class SearchField(QLineEdit):
    def __init__(self, placeholder: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setAccessibleName(placeholder or tr('kit.03c481a6ab85'))
        self.setObjectName("search")
        self._lead = self.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        self._clear = self.addAction(QIcon(), QLineEdit.ActionPosition.TrailingPosition)
        self.retheme()
        self._clear.setToolTip(tr('kit.bce2377283c2'))
        self._clear.setVisible(False)
        self._clear.triggered.connect(self.clear)
        self.textChanged.connect(lambda text: self._clear.setVisible(bool(text)))

    def retheme(self) -> None:
        self._lead.setIcon(icons.icon("search", C.text_muted, 16))
        self._clear.setIcon(icons.icon("x", C.text_secondary, 14))

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self.text():
            self.clear()
            event.accept()
            return
        super().keyPressEvent(event)


# --------------------------------------------------------------------------- filter chips


class FilterChip(KbFocus, Hover, QAbstractButton):
    def __init__(self, key: str, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._init_hover()
        self.key, self._label, self._count = key, text, None
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFont(font(T.meta + 1, QFont.Weight.Medium))
        self.setFixedHeight(30)
        self.setAccessibleName(text)

    def set_count(self, count: int | None) -> None:
        self._count = count
        self.setAccessibleName(f"{self._label} {count}" if count is not None else self._label)
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = QFontMetrics(self.font())
        width = 14 + fm.horizontalAdvance(self._label) + 14
        if self._count is not None:
            width += 6 + QFontMetrics(font(T.micro)).horizontalAdvance(str(self._count))
        return QSize(width, 30)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        checked = self.isChecked()
        if checked:
            p.setBrush(qcolor(C.accent, ACCENT_SUBTLE_A))
            p.setPen(QPen(qcolor(C.accent, 0.35), 1))
        else:
            p.setBrush(qcolor(C.border, 0.10 * self._hv))
            p.setPen(QPen(qcolor(C.border, 0.0 if self._hv == 0 else 0.0), 1))
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), r.height() / 2, r.height() / 2)
        fg = QColor(C.accent) if checked else lerp_color(C.text_secondary, C.text, self._hv)
        p.setFont(self.font())
        p.setPen(fg)
        fm = QFontMetrics(self.font())
        x = 14
        p.drawText(QRectF(x, 0, fm.horizontalAdvance(self._label) + 2, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._label)
        if self._count is not None:
            p.setFont(font(T.micro))
            p.setPen(QColor(C.accent) if checked else QColor(C.text_muted))
            p.drawText(QRectF(x + fm.horizontalAdvance(self._label) + 6, 0, 40, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, str(self._count))
        if self._kb:
            draw_focus_ring(p, r, r.height() / 2)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        bar = self.parent()
        if isinstance(bar, FilterBar) and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            bar.step(-1 if event.key() == Qt.Key.Key_Left else 1)
            return
        super().keyPressEvent(event)


class FilterBar(QWidget):
    """Single-select segmented filter. Roving tab stop; Left/Right change selection."""

    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]], parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(S.xs + 2)
        self.chips: dict[str, FilterChip] = {}
        for key, text in options:
            chip = FilterChip(key, text, self)
            chip.clicked.connect(lambda _=False, k=key: self.set_current(k))
            layout.addWidget(chip)
            self.chips[key] = chip
        layout.addStretch(1)
        self._current = options[0][0]
        self.set_current(self._current, emit=False)

    def current(self) -> str:
        return self._current

    def set_current(self, key: str, emit: bool = True) -> None:
        if key not in self.chips:
            return
        changed = key != self._current
        self._current = key
        for name, chip in self.chips.items():
            chip.setChecked(name == key)
            chip.setFocusPolicy(Qt.FocusPolicy.StrongFocus if name == key else Qt.FocusPolicy.ClickFocus)
        if changed and emit:
            self.changed.emit(key)

    def set_counts(self, counts: dict[str, int]) -> None:
        for key, chip in self.chips.items():
            chip.set_count(counts.get(key))

    def step(self, delta: int) -> None:
        keys = list(self.chips)
        key = keys[(keys.index(self._current) + delta) % len(keys)]
        self.set_current(key)
        self.chips[key].setFocus(Qt.FocusReason.TabFocusReason)


# --------------------------------------------------------------------------- sections


class Section(QFrame):
    """Flat surface block with a title row; the body is a plain vertical layout."""

    def __init__(self, title: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("surface", "card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        root = QVBoxLayout(self)
        root.setContentsMargins(S.lg + 2, S.lg, S.lg + 2, S.lg + 2)
        root.setSpacing(S.md)
        self.head = QHBoxLayout()
        self.head.setSpacing(S.sm)
        self.title = QLabel(title)
        self.title.setProperty("role", "section")
        self.head.addWidget(self.title, 1)
        root.addLayout(self.head)
        self.body = QVBoxLayout()
        self.body.setSpacing(S.md)
        root.addLayout(self.body)
        if not title:
            self.title.hide()

    def add_head_widget(self, widget: QWidget) -> None:
        self.head.addWidget(widget)


class _CollapseHeader(KbFocus, Hover, QAbstractButton):
    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._init_hover()
        self._title = title
        self._open = False
        self.setCheckable(True)
        self.setFixedHeight(Size.hit + 4)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        self.setFont(font(T.body, QFont.Weight.DemiBold))

    def set_open(self, value: bool) -> None:
        self._open = value
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(200, Size.hit + 4)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        if self._hv > 0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(C.border, 0.07 * self._hv))
            p.drawRoundedRect(r, R.lg, R.lg)
        fg = lerp_color(C.text_secondary, C.text, self._hv)
        p.setFont(self.font())
        p.setPen(fg)
        p.drawText(QRectF(S.lg + 2, 0, r.width() - 64, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._title)
        draw_icon(p, self, "chevron-down" if self._open else "chevron-right", fg, r.width() - S.lg - 2 - 16, (r.height() - 16) / 2, 16)
        if self._kb:
            draw_focus_ring(p, r, R.lg)


class Collapsible(QFrame):
    """Progressive-disclosure block for low-frequency details (closed by default)."""

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("surface", "card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.header = _CollapseHeader(title)
        self.header.toggled.connect(self._toggle)
        root.addWidget(self.header)
        self.clip = QWidget()
        self.clip.setMaximumHeight(0)
        self.clip.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.content = QWidget(self.clip)
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(S.lg + 2, 0, S.lg + 2, S.lg + 2)
        self.body.setSpacing(S.md)
        inner = QVBoxLayout(self.clip)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.addWidget(self.content)
        root.addWidget(self.clip)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(M.panel)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(lambda v: self.clip.setMaximumHeight(int(v)))
        self._anim.finished.connect(self._done)
        self._open = False

    def is_open(self) -> bool:
        return self._open

    def set_open(self, value: bool, animate: bool = True) -> None:
        if value == self._open:
            return
        self.header.blockSignals(True)
        self.header.setChecked(value)
        self.header.blockSignals(False)
        self._toggle(value, animate)

    def _toggle(self, value: bool, animate: bool = True) -> None:
        self._open = value
        self.header.set_open(value)
        self._anim.stop()
        target = self.content.sizeHint().height() if value else 0
        if animate and self.isVisible():
            self._anim.setStartValue(self.clip.maximumHeight() if self.clip.maximumHeight() < QWIDGETSIZE_MAX else target)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self.clip.setMaximumHeight(QWIDGETSIZE_MAX if value else 0)

    def _done(self) -> None:
        if self._open:
            self.clip.setMaximumHeight(QWIDGETSIZE_MAX)


class KeyValueRow(QWidget):
    """``label    value`` row. Values elide (tooltip carries the full text); optional copy button."""

    def __init__(self, key: str, value: str = "", *, mono: bool = False, copyable: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self._mono, self._copyable = mono, copyable
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(S.md)
        self.key_label = ElidedLabel(key, px=T.body, color="text_secondary")
        self.key_label.setFixedWidth(104)
        row.addWidget(self.key_label, 0, Qt.AlignmentFlag.AlignTop)
        self.values = QVBoxLayout()
        self.values.setContentsMargins(0, 0, 0, 0)
        self.values.setSpacing(2)
        row.addLayout(self.values, 1)
        self.copy_button: IconButton | None = None
        if copyable:
            self.copy_button = IconButton("copy", tr('kit.bb6dad578c81'), size=24, icon_size=14)
            self.copy_button.clicked.connect(self._copy)
            row.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignTop)
        self._widgets: list[QWidget] = []
        self.set_value(value)

    def set_value(self, value: str | list[str], color: str = "text", mono: bool | None = None) -> None:
        for w in self._widgets:
            self.values.removeWidget(w)
            dispose_widget(w)
        self._widgets = []
        lines = value if isinstance(value, list) else [value]
        self._text = "\n".join(lines)
        use_mono = self._mono if mono is None else mono
        for line in lines or [""]:
            lab = ElidedLabel(line, px=T.body if not use_mono else T.meta + 1, color=color, mono=use_mono)
            self.values.addWidget(lab)
            self._widgets.append(lab)
        if self.copy_button is not None:
            self.copy_button.setVisible(bool(self._text.strip()))

    def value_text(self) -> str:
        return self._text

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._text)
        if self.copy_button is not None:
            self.copy_button.set_icon("check")
            later(self, 1200, lambda: self.copy_button and self.copy_button.set_icon("copy"))


class FactList(QWidget):
    """Grouped ``label  value`` rows (a details panel), or a single message when there is nothing to list.

    ``rows`` items are ``(key, value)``, ``(key, value, True)`` for a copy button, or ``("#", heading)``.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(S.sm)
        self._text = ""

    def _clear(self) -> None:
        while self.box.count():
            item = self.box.takeAt(0)
            if item.widget() is not None:
                dispose_widget(item.widget())

    def set_rows(self, rows: list[tuple]) -> None:
        self._clear()
        lines = []
        for row in rows:
            if row[0] == "#":
                heading = QLabel(row[1])
                heading.setProperty("role", "subsection")
                self.box.addSpacing(S.xs)
                self.box.addWidget(heading)
                lines.append(row[1])
                continue
            key, value, *copy = row
            self.box.addWidget(KeyValueRow(key, value, copyable=bool(copy and copy[0])))
            lines.append(f"{key} {value}")
        self._text = "\n".join(lines)

    def set_message(self, text: str) -> None:
        self._clear()
        message = QLabel(text)
        message.setProperty("role", "muted")
        message.setWordWrap(True)
        self.box.addWidget(message)
        self._text = text

    def setText(self, text: str) -> None:  # noqa: N802 - QLabel-compatible
        self.set_message(text)

    def text(self) -> str:
        return self._text


# --------------------------------------------------------------------------- empty state


class EmptyState(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(S.xl, S.xxl, S.xl, S.xxl)
        root.setSpacing(S.md)
        root.addStretch(1)
        self.tile = _IconTile(56, 28, radius=R.xl)
        root.addWidget(self.tile, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addSpacing(S.xs)
        self.title = QLabel()
        self.title.setProperty("role", "dialogTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.title.setWordWrap(True)
        root.addWidget(self.title)
        self.message = QLabel()
        self.message.setProperty("role", "muted")
        self.message.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.message.setWordWrap(True)
        root.addWidget(self.message, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addSpacing(S.sm)
        self.primary = make_button("", "primary", "plus")
        self.secondary = make_button("", "subtle")
        root.addWidget(self.primary, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(self.secondary, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addStretch(2)

    def set_content(self, icon_name: str, title: str, message: str, primary: str = "", secondary: str = "") -> None:
        self.tile.set_icon(icon_name)
        self.title.setText(title)
        self.message.setText(message)
        self.primary.setText(primary)
        self.primary.setVisible(bool(primary))
        self.secondary.setText(secondary)
        self.secondary.setVisible(bool(secondary))
        self._fit_labels()

    def _fit_labels(self) -> None:
        width = max(160, min(380, self.width() - 2 * S.xl))
        for lab in (self.title, self.message):
            lab.setFixedWidth(width)
            lab.setMinimumHeight(lab.heightForWidth(width))
            lab.setMaximumHeight(lab.heightForWidth(width))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit_labels()


class _IconTile(QWidget):
    """Rounded square holding a glyph (environment kind, security state, empty state...)."""

    def __init__(self, size: int = 40, icon_size: int = 20, radius: int = R.lg, parent: QWidget | None = None):
        super().__init__(parent)
        self._size, self._icon_size, self._radius = size, icon_size, radius
        self._name, self._color, self._bg = "server", "text_secondary", ("border", 0.12)
        self._project: str | None = None
        self._custom: tuple[dict, str, str] | None = None
        self.setFixedSize(size, size)

    def set_project(self, project_id: str) -> None:
        """Show this project's own icon (glyph, SVG or picture; see ``project_icons``)."""
        self._project, self._custom = project_id, None
        self.update()

    def set_custom(self, entry: dict | None, hue: str, glyph: str) -> None:
        """Show a chosen icon entry (a workspace's), with ``glyph`` in ``hue`` when nothing was chosen."""
        self._project, self._custom = None, (dict(entry or {}), hue, glyph)
        self.update()

    def set_icon(self, name: str, color: str = "accent", bg: tuple[str, float] | None = None) -> None:
        """``color`` and ``bg`` (colour, alpha) take theme token names so the tile follows a theme switch."""
        self._project, self._custom = None, None
        self._name, self._color = name, color
        self._bg = (color, TINT_A) if bg is None else bg
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._project is not None or self._custom is not None:
            from . import project_icons
            entry, hue, glyph = self._custom if self._custom is not None else (None, "", "folder")
            project_icons.draw(p, QRectF(self.rect()), self._project or "", entry=entry, hue=hue, glyph=glyph,
                               dpr=max(2.0, self.devicePixelRatioF()), radius=self._radius)
            return
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcolor(resolve(self._bg[0]), self._bg[1]))
        p.drawRoundedRect(QRectF(self.rect()), self._radius, self._radius)
        s = self._icon_size
        draw_icon(p, self, self._name, self._color, (self.width() - s) / 2, (self.height() - s) / 2, s)


IconTile = _IconTile


# --------------------------------------------------------------------------- overlays: action menu


@dataclass
class MenuAction:
    text: str
    callback: Callable[[], Any] | None
    icon: str = ""
    danger: bool = False
    enabled: bool = True
    separator_before: bool = False
    hint: str = ""


class _MenuItem(KbFocus, Hover, QAbstractButton):
    def __init__(self, action: MenuAction, parent: QWidget | None = None):
        super().__init__(parent)
        self._init_hover()
        self.action = action
        self.setFixedHeight(Size.control)
        self.setEnabled(action.enabled)
        self.setCursor(Qt.CursorShape.PointingHandCursor if action.enabled else Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(action.text)
        self.setFont(font(T.body, QFont.Weight.Medium))

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = QFontMetrics(self.font())
        return QSize(40 + fm.horizontalAdvance(self.action.text) + 28, Size.control)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        highlight = self.hasFocus() or self._hv > 0
        if self.isEnabled() and highlight:
            amount = 1.0 if self.hasFocus() else self._hv
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(C.danger, 0.14 * amount) if self.action.danger else qcolor(C.border, 0.14 * amount))
            p.drawRoundedRect(r, R.sm, R.sm)
        if not self.isEnabled():
            fg = QColor(C.text_disabled)
        else:
            fg = QColor(C.danger if self.action.danger else C.text)
        if self.action.icon:
            draw_icon(p, self, self.action.icon, QColor(C.danger) if (self.action.danger and self.isEnabled()) else (C.text_secondary if self.isEnabled() else C.text_disabled), 12, (r.height() - 16) / 2, 16)
        p.setFont(self.font())
        p.setPen(fg)
        p.drawText(QRectF(36, 0, r.width() - 36 - 12, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.action.text)


class ActionMenu(QFrame):
    """Popup overflow menu with icons, a red destructive tier and full keyboard support."""

    about_to_show = Signal()
    about_to_hide = Signal()

    def __init__(self, actions: list[MenuAction], parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._panel = QFrame(self)
        self._panel.setObjectName("menuPanel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 10)
        outer.addWidget(self._panel)
        layout = QVBoxLayout(self._panel)
        layout.setContentsMargins(S.xs + 2, S.xs + 2, S.xs + 2, S.xs + 2)
        layout.setSpacing(2)
        self.items: list[_MenuItem] = []
        for action in actions:
            if action.separator_before and self.items:
                rule = QFrame()
                rule.setFixedHeight(1)
                rule.setObjectName("menuRule")
                layout.addSpacing(S.xs)
                layout.addWidget(rule)
                layout.addSpacing(S.xs)
            item = _MenuItem(action)
            item.clicked.connect(lambda _=False, a=action: self._trigger(a))
            layout.addWidget(item)
            self.items.append(item)
        shadow = QGraphicsDropShadowEffect(self._panel)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 120))
        self._panel.setGraphicsEffect(shadow)

    def _trigger(self, action: MenuAction) -> None:
        self.close()
        if action.callback is not None:
            later(self, 0, action.callback)

    def popup_below(self, anchor: QWidget) -> None:
        self.adjustSize()
        size = self.sizeHint()
        origin = anchor.mapToGlobal(QPoint(anchor.width(), anchor.height() + 4))
        pos = QPoint(origin.x() - size.width() + 6, origin.y() - 6)
        screen = QGuiApplication.screenAt(origin) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        if pos.y() + size.height() > area.bottom():
            pos.setY(anchor.mapToGlobal(QPoint(0, 0)).y() - size.height() + 6)
        pos.setX(max(area.left() + 4, min(pos.x(), area.right() - size.width() - 4)))
        self.move(pos)
        self.show()
        for item in self.items:
            if item.isEnabled():
                item.setFocus(Qt.FocusReason.PopupFocusReason)
                break

    def showEvent(self, event) -> None:  # noqa: N802
        self.about_to_show.emit()
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.about_to_hide.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        enabled = [i for i in self.items if i.isEnabled()]
        if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up) and enabled:
            current = next((n for n, i in enumerate(enabled) if i.hasFocus()), -1)
            step = 1 if event.key() == Qt.Key.Key_Down else -1
            enabled[(current + step) % len(enabled)].setFocus(Qt.FocusReason.TabFocusReason)
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            focused = next((i for i in enabled if i.hasFocus()), None)
            if focused is not None:
                focused.click()
                return
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)


# --------------------------------------------------------------------------- toast


class Toast(QFrame):
    """One message. Hovering pauses its timer; dragging moves the whole stack; right-click offers the default place."""

    closed = Signal(object)
    dragged = Signal(object, QPoint)     # toast, new top-left in the host
    drag_finished = Signal()
    reset_requested = Signal()

    def __init__(self, parent: QWidget, message: str, tone: str, duration_ms: int):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        fg = TONES.get(tone, C.accent if tone == "info" else C.neutral)
        self.setMaximumWidth(380)
        row = QHBoxLayout(self)
        row.setContentsMargins(S.md + 2, S.md, S.sm, S.md)
        row.setSpacing(S.md)
        glyph = {"ok": "check-circle", "warn": "alert", "danger": "x-circle"}.get(tone, "info")
        self._icon = _IconTile(28, 16, radius=R.md)
        self._icon.set_icon(glyph, fg)
        row.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)
        self.text = QLabel(message)
        self.text.setWordWrap(True)
        self.text.setMinimumWidth(180)
        self.text.setObjectName("toastText")
        row.addWidget(self.text, 1)
        self.close_button = IconButton("x", tr('kit.641c2d091bbc'), size=28, icon_size=14)
        self.close_button.clicked.connect(self.dismiss)
        row.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        self.setAccessibleName((tr('kit.7f9bba587ace') if tone == "ok" else tr('kit.f78e031368d5') if tone == "danger" else tr('kit.4c8478387565') if tone == "warn" else "") + message)
        self.effect = QGraphicsOpacityEffect(self)
        self.effect.setOpacity(0.0)
        self.setGraphicsEffect(self.effect)
        self.fade = QPropertyAnimation(self.effect, b"opacity", self)
        self.fade.setDuration(M.toast)
        self.slide = QPropertyAnimation(self, b"pos", self)
        self.slide.setDuration(M.toast)
        self.slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._dismissing = False
        self._sticky = duration_ms <= 0           # errors stay until closed, so they cannot be missed
        self._grab: QPoint | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(max(1, duration_ms))
        self._timer.timeout.connect(self.dismiss)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip(tr("Drag to move messages. Right-click to put them back."))

    def fit(self, width: int) -> None:
        self.setFixedWidth(width)
        self.setFixedHeight(max(48, self.layout().totalHeightForWidth(width)))

    def enter(self, to_pos: QPoint) -> None:
        self.move(to_pos - QPoint(0, 14))
        self.show()
        self.fade.stop()
        self.fade.setStartValue(0.0)
        self.fade.setEndValue(1.0)
        self.fade.start()
        self.move_to(to_pos)
        if not self._sticky:
            self._timer.start()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()                       # reading it: do not let it vanish mid-sentence
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if not self._sticky and not self._dismissing:
            self._timer.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._grab = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.RightButton:
            menu = QMenu(self)
            menu.addAction(tr("Put messages back at the top"), self.reset_requested.emit)
            menu.exec(event.globalPosition().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._grab is not None:
            self.slide.stop()
            self.dragged.emit(self, self.mapToParent(event.position().toPoint() - self._grab))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._grab is not None and event.button() == Qt.MouseButton.LeftButton:
            self._grab = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.drag_finished.emit()
        super().mouseReleaseEvent(event)

    def move_to(self, pos: QPoint) -> None:
        """Glide to ``pos`` (used for entrance and when the stack reflows)."""
        if self.pos() == pos:
            return
        self.slide.stop()
        self.slide.setStartValue(self.pos())
        self.slide.setEndValue(pos)
        self.slide.start()

    def dismiss(self) -> None:
        if self._dismissing:
            return
        self._dismissing = True
        self._timer.stop()
        self.fade.stop()
        self.fade.setStartValue(self.effect.opacity())
        self.fade.setEndValue(0.0)
        self.fade.finished.connect(lambda: self.closed.emit(self))
        self.fade.start()


class ToastManager(QObject):
    """Stack of messages at the top centre, where they are hard to miss. Errors stay until closed.

    The stack can be dragged anywhere; ``anchor`` is where its top-centre sits as fractions of the window, so it
    survives resizing. ``moved`` reports a new anchor (None = back to the default) for the window to remember.
    """

    moved = Signal(object)

    def __init__(self, host: QWidget, anchor: tuple[float, float] | None = None):
        super().__init__(host)
        self.host = host
        self.toasts: list[Toast] = []
        self.anchor = anchor
        host.installEventFilter(self)

    def show(self, message: str, tone: str = "info", duration_ms: int | None = None) -> Toast:
        duration = duration_ms if duration_ms is not None else {"danger": 0, "warn": 6000}.get(tone, 4000)
        toast = Toast(self.host, message, tone, duration)
        toast.closed.connect(self._remove)
        toast.dragged.connect(self._drag)
        toast.drag_finished.connect(lambda: self.moved.emit(self.anchor))
        toast.reset_requested.connect(self.reset_position)
        self.toasts.append(toast)
        while len(self.toasts) > 3:
            self.toasts[0].dismiss()
            self._remove(self.toasts[0])
        toast.fit(min(380, max(240, self.host.width() - 2 * S.lg)))
        toast.enter(self._slot(len(self.toasts) - 1, toast))
        toast.raise_()
        self._restack(animate=True)
        return toast

    def _origin(self, width: int) -> QPoint:
        """Top-left of the first message: the remembered anchor, else centred near the top; always on screen."""
        if self.anchor is None:
            x, y = (self.host.width() - width) // 2, S.lg
        else:
            x = round(self.anchor[0] * self.host.width() - width / 2)
            y = round(self.anchor[1] * self.host.height())
        total = sum(t.height() + S.sm for t in self.toasts)
        x = max(S.sm, min(x, self.host.width() - width - S.sm))
        y = max(S.sm, min(y, self.host.height() - max(total, 48) - S.sm))
        return QPoint(x, y)

    def _slot(self, index: int, toast: Toast) -> QPoint:
        origin = self._origin(toast.width())
        y = origin.y() + sum(other.height() + S.sm for other in self.toasts[:index])
        return QPoint(origin.x(), y)

    def _drag(self, toast: Toast, top_left: QPoint) -> None:
        index = self.toasts.index(toast) if toast in self.toasts else 0
        first_top = top_left.y() - sum(other.height() + S.sm for other in self.toasts[:index])
        width, height = max(1, self.host.width()), max(1, self.host.height())
        self.anchor = (min(1.0, max(0.0, (top_left.x() + toast.width() / 2) / width)), min(1.0, max(0.0, first_top / height)))
        self._restack(animate=False)

    def reset_position(self) -> None:
        self.anchor = None
        self._restack(animate=True)
        self.moved.emit(None)

    def _restack(self, animate: bool = True) -> None:
        for index, toast in enumerate(self.toasts):
            if toast._dismissing:
                continue
            pos = self._slot(index, toast)
            if animate:
                toast.move_to(pos)
            else:
                toast.slide.stop()
                toast.move(pos)

    def _remove(self, toast: Toast) -> None:
        if toast in self.toasts:
            self.toasts.remove(toast)
            toast.hide()
            toast.deleteLater()
            self._restack()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.host and event.type() == QEvent.Type.Resize:
            for toast in self.toasts:
                toast.fit(min(380, max(240, self.host.width() - 2 * S.lg)))
            self._restack(animate=False)
        return False


# --------------------------------------------------------------------------- confirm dialog


class ConfirmDialog(QDialog):
    """Modal confirmation with a clear safe default. Destructive confirmations are red."""

    def __init__(self, parent: QWidget | None, title: str, message: str, *, confirm_text: str = tr('kit.dce52f3e1119'),
                 cancel_text: str = tr('kit.2cd0f3be8738'), danger: bool = False, icon_name: str | None = None,
                 dont_ask: bool = False):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setWindowTitle(title)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 20, 28, 36)
        panel = QFrame()
        panel.setObjectName("confirmPanel")
        panel.setMinimumWidth(380)
        panel.setMaximumWidth(460)
        shadow = QGraphicsDropShadowEffect(panel)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 12)
        shadow.setColor(QColor(0, 0, 0, 150))
        panel.setGraphicsEffect(shadow)
        outer.addWidget(panel)
        body = QVBoxLayout(panel)
        body.setContentsMargins(S.xl, S.xl, S.xl, S.lg + 2)
        body.setSpacing(S.md)
        head = QHBoxLayout()
        head.setSpacing(S.md + 2)
        tile = _IconTile(40, 20)
        tile.set_icon(icon_name or ("alert" if danger else "info"), "danger" if danger else "accent")
        head.addWidget(tile, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(S.xs + 2)
        heading = QLabel(title)
        heading.setProperty("role", "section")
        heading.setWordWrap(True)
        self.message = QLabel(message)
        self.message.setProperty("role", "muted")
        self.message.setWordWrap(True)
        self.message.setMinimumWidth(240)
        text.addWidget(heading)
        text.addWidget(self.message)
        head.addLayout(text, 1)
        body.addLayout(head)
        body.addSpacing(S.sm)
        self.dont_ask = None
        if dont_ask:                       # routine confirmations the user may switch off (back on in Settings)
            from PySide6.QtWidgets import QCheckBox
            self.dont_ask = QCheckBox(tr("Don't ask again"))
            body.addWidget(self.dont_ask)
        buttons = QHBoxLayout()
        buttons.setSpacing(S.sm)
        buttons.addStretch(1)
        self.cancel_button = make_button(cancel_text, "ghost")
        self.confirm_button = make_button(confirm_text, "danger" if danger else "primary")
        self.cancel_button.clicked.connect(self.reject)
        self.confirm_button.clicked.connect(self.accept)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.confirm_button)
        body.addLayout(buttons)
        self.setTabOrder(self.confirm_button, self.cancel_button)
        # Destructive actions default to the safe choice; routine ones default to confirm.
        (self.cancel_button if danger else self.confirm_button).setFocus()

    @staticmethod
    def ask(parent: QWidget | None, title: str, message: str, **kwargs: Any) -> bool:
        dialog = ConfirmDialog(parent, title, message, **kwargs)
        if parent is not None:
            window = parent.window()
            dialog.adjustSize()
            center = window.geometry().center()
            dialog.move(center.x() - dialog.width() // 2, center.y() - dialog.height() // 2)
        return dialog.exec() == QDialog.DialogCode.Accepted

    @staticmethod
    def ask_skippable(parent: QWidget | None, title: str, message: str, **kwargs: Any) -> tuple[bool, bool]:
        """(confirmed, don't ask again): the box is only honoured together with a confirmation."""
        dialog = ConfirmDialog(parent, title, message, dont_ask=True, **kwargs)
        if parent is not None:
            window = parent.window()
            dialog.adjustSize()
            center = window.geometry().center()
            dialog.move(center.x() - dialog.width() // 2, center.y() - dialog.height() // 2)
        confirmed = dialog.exec() == QDialog.DialogCode.Accepted
        return confirmed, confirmed and dialog.dont_ask.isChecked()


# --------------------------------------------------------------------------- command palette


@dataclass
class Command:
    id: str
    title: str
    subtitle: str = ""
    icon: str = "command"
    group: str = ""
    callback: Callable[[], Any] | None = None
    keywords: str = ""
    enabled: bool = True


class _CommandDelegate(QStyledItemDelegate):
    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), 48)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        cmd: Command = index.data(Qt.ItemDataRole.UserRole)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(6, 2, -6, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(C.accent, ACCENT_SUBTLE_A))
            painter.drawRoundedRect(rect, R.md, R.md)
            painter.setBrush(qcolor(C.accent))
            painter.drawRoundedRect(QRectF(rect.left(), rect.center().y() - 9, 3, 18), 1.5, 1.5)
        color = C.text if cmd.enabled else C.text_disabled
        painter.drawPixmap(round(rect.left() + 14), round(rect.center().y() - 9),
                           icons.pixmap(cmd.icon, C.accent if selected and cmd.enabled else (C.text_secondary if cmd.enabled else C.text_disabled), 18, 2.0))
        title_font = font(T.body + 1, QFont.Weight.Medium)
        painter.setFont(title_font)
        painter.setPen(QColor(color))
        text_left = rect.left() + 44
        hint_width = QFontMetrics(font(T.micro)).horizontalAdvance(cmd.group) + 8 if cmd.group else 0
        avail = rect.width() - 44 - 14 - hint_width
        if cmd.subtitle:
            painter.drawText(QRectF(text_left, rect.top() + 5, avail, 20), Qt.AlignmentFlag.AlignVCenter,
                             QFontMetrics(title_font).elidedText(cmd.title, Qt.TextElideMode.ElideRight, int(avail)))
            sub_font = font(T.meta)
            painter.setFont(sub_font)
            painter.setPen(QColor(C.text_muted))
            painter.drawText(QRectF(text_left, rect.top() + 24, avail, 16), Qt.AlignmentFlag.AlignVCenter,
                             QFontMetrics(sub_font).elidedText(cmd.subtitle, Qt.TextElideMode.ElideRight, int(avail)))
        else:
            painter.drawText(QRectF(text_left, rect.top(), avail, rect.height()), Qt.AlignmentFlag.AlignVCenter,
                             QFontMetrics(title_font).elidedText(cmd.title, Qt.TextElideMode.ElideRight, int(avail)))
        if cmd.group:
            painter.setFont(font(T.micro))
            painter.setPen(QColor(C.text_muted))
            painter.drawText(QRectF(rect.right() - hint_width - 10, rect.top(), hint_width, rect.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, cmd.group)
        painter.restore()


class CommandPalette(QDialog):
    """Ctrl+K quick actions: type to filter, Up/Down to move, Enter to run, Esc to close."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle(tr('kit.cb792ee76044'))
        self._commands: list[Command] = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 12, 28, 36)
        panel = QFrame()
        panel.setObjectName("palettePanel")
        shadow = QGraphicsDropShadowEffect(panel)
        shadow.setBlurRadius(48)
        shadow.setOffset(0, 16)
        shadow.setColor(QColor(0, 0, 0, 160))
        panel.setGraphicsEffect(shadow)
        outer.addWidget(panel)
        root = QVBoxLayout(panel)
        root.setContentsMargins(0, 0, 0, S.sm)
        root.setSpacing(0)
        top = QHBoxLayout()
        top.setContentsMargins(S.lg + 2, S.md, S.lg, S.md)
        top.setSpacing(S.md)
        top.addWidget(glyph_label("search", "text_muted", 18))
        self.input = QLineEdit()
        self.input.setPlaceholderText(tr('kit.2cc1cfa1592f'))
        self.input.setAccessibleName(tr('kit.a2402eaab073'))
        self.input.setObjectName("paletteInput")
        self.input.textChanged.connect(self._refilter)
        self.input.installEventFilter(self)
        top.addWidget(self.input, 1)
        esc = QLabel("Esc")
        esc.setProperty("role", "meta")
        top.addWidget(esc)
        root.addLayout(top)
        rule = QFrame()
        rule.setFixedHeight(1)
        rule.setObjectName("divider")
        root.addWidget(rule)
        self.list = QListWidget()
        self.list.setItemDelegate(_CommandDelegate(self.list))
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setMinimumHeight(280)
        self.list.setMaximumHeight(360)
        self.list.itemClicked.connect(lambda _: self._run())
        root.addWidget(self.list)
        self.empty = QLabel(tr('kit.8995a50deb57'))
        self.empty.setProperty("role", "muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setMinimumHeight(120)
        root.addWidget(self.empty)
        foot = QLabel(tr('kit.96696369b501'))
        foot.setProperty("role", "meta")
        foot.setContentsMargins(S.lg + 2, S.sm, 0, 0)
        root.addWidget(foot)

    def set_commands(self, commands: list[Command]) -> None:
        self._commands = commands

    def open_palette(self) -> None:
        self.input.clear()
        self._refilter()
        parent = self.parentWidget()
        width = min(600, (parent.width() if parent else 600) - 32)
        self.setFixedWidth(max(360, width) + 56)
        self.adjustSize()
        if parent is not None:
            window = parent.window().geometry()
            self.move(window.center().x() - self.width() // 2, window.top() + max(40, int(window.height() * 0.14)))
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    def visible_commands(self) -> list[Command]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]

    def _refilter(self) -> None:
        tokens = self.input.text().casefold().split()
        rows = []
        for cmd in self._commands:
            haystack = f"{cmd.title} {cmd.subtitle} {cmd.group} {cmd.keywords}".casefold()
            if all(t in haystack for t in tokens):
                rank = 0 if tokens and cmd.title.casefold().startswith(tokens[0]) else 1
                rows.append((rank, cmd))
        rows.sort(key=lambda pair: pair[0])
        self.list.clear()
        for _, cmd in rows:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, cmd)
            if not cmd.enabled:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(item)
        first = next((i for i in range(self.list.count()) if self.list.item(i).flags() & Qt.ItemFlag.ItemIsEnabled), -1)
        if first >= 0:
            self.list.setCurrentRow(first)
        self.list.setVisible(bool(rows))
        self.empty.setVisible(not rows)

    def _move(self, delta: int) -> None:
        count = self.list.count()
        if not count:
            return
        row = self.list.currentRow()
        for _ in range(count):
            row = (row + delta) % count
            if self.list.item(row).flags() & Qt.ItemFlag.ItemIsEnabled:
                self.list.setCurrentRow(row)
                return

    def _run(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        cmd: Command = item.data(Qt.ItemDataRole.UserRole)
        if not cmd.enabled:
            return
        self.close()
        if cmd.callback is not None:
            later(self, 0, cmd.callback)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Down:
                self._move(1)
                return True
            if key == Qt.Key.Key_Up:
                self._move(-1)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._run()
                return True
        return super().eventFilter(watched, event)

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow() and self.isVisible():
            later(self, 0, lambda: self.isVisible() and not self.isActiveWindow() and self.close())


BADGE_ROLE = int(Qt.ItemDataRole.UserRole) + 1   # item data: (label, tone) drawn as a status pill on the right
ICON_ROLE = int(Qt.ItemDataRole.UserRole) + 5    # item data: (icon name, colour token) drawn as a quiet tile on the left
GROUP_ROLE = int(Qt.ItemDataRole.UserRole) + 6   # item data: group key; the row is a heading drawn by its own widget


class TwoLineDelegate(QStyledItemDelegate):
    """First line is the title; remaining lines are quiet metadata. Gives plain lists a visible hierarchy.
    An optional ``BADGE_ROLE`` value draws a semantic status pill (running / stopped / failed ...)."""

    def _lines(self, index) -> list[str]:
        return str(index.data(Qt.ItemDataRole.DisplayRole) or "").split("\n")

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # noqa: N802
        if index.data(GROUP_ROLE) is not None:
            return QSize(option.rect.width(), 44 + 2 * (S.md + 2))   # the list's item padding + margin sit around the widget
        extra = min(len(self._lines(index)) - 1, 2)
        return QSize(option.rect.width(), 36 + 18 * max(extra, 0) + (2 if extra else 0))

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        if index.data(GROUP_ROLE) is not None:
            return                                   # a group heading: its item widget does the drawing
        lines = self._lines(index)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(2, 1, -2, -1)
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
        left, width = rect.left() + 16, rect.width() - 28
        glyph = index.data(ICON_ROLE)
        if glyph and glyph[0] == "@project":                 # a project's own icon (project_icons.signature)
            from . import project_icons
            project_icons.draw(painter, QRectF(rect.left() + 12, rect.center().y() - 15, 30, 30), glyph[1], selected=selected)
            left, width = rect.left() + 54, rect.width() - 66
        elif glyph:
            name, token = glyph
            tint = resolve(token)
            tile = QRectF(rect.left() + 12, rect.center().y() - 15, 30, 30)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(tint, TINT_STRONG_A if selected else TINT_A))
            painter.drawRoundedRect(tile, R.md, R.md)
            painter.drawPixmap(round(tile.left() + 7), round(tile.top() + 7), icons.pixmap(name, tint, 16, 2.0))
            left, width = rect.left() + 54, rect.width() - 66
        badge = index.data(BADGE_ROLE)
        if badge:
            text, tone = badge
            badge_font = font(T.meta, QFont.Weight.Medium)
            badge_fm = QFontMetrics(badge_font)
            badge_w = badge_fm.horizontalAdvance(text) + 14 + 12 + 6
            pill = QRectF(rect.right() - 14 - badge_w, rect.center().y() - 11, badge_w, 22)
            fg = TONES.get(tone, C.neutral)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(C.border if tone == "unknown" else fg, 0.14))
            painter.drawRoundedRect(pill, 11, 11)
            painter.setBrush(QColor(fg))
            painter.drawEllipse(QRectF(pill.left() + 9, pill.center().y() - 3, 6, 6))
            painter.setFont(badge_font)
            painter.setPen(QColor(C.text_secondary if tone == "unknown" else fg))
            painter.drawText(QRectF(pill.left() + 20, pill.top(), badge_w - 24, pill.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
            width -= badge_w + 12
        title_font = font(T.body + 1, QFont.Weight.Medium)
        painter.setFont(title_font)
        painter.setPen(QColor(C.text))
        top = rect.top() + (10 if len(lines) > 1 else 0)
        height = 20 if len(lines) > 1 else rect.height()
        painter.drawText(QRectF(left, top, width, height), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         QFontMetrics(title_font).elidedText(lines[0], Qt.TextElideMode.ElideRight, int(width)))
        sub_font = font(T.meta)
        painter.setFont(sub_font)
        painter.setPen(QColor(C.text_muted))
        for n, line in enumerate(lines[1:3]):
            painter.drawText(QRectF(left, top + 21 + 17 * n, width, 16), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             QFontMetrics(sub_font).elidedText(line, Qt.TextElideMode.ElideMiddle, int(width)))
        painter.restore()


# --------------------------------------------------------------------------- split body with drawer


class _DrawerSurface(QWidget):
    """Hosts the detail panel; paints an opaque rounded sheet only while acting as a drawer."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.drawer = False
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)

    def set_drawer(self, value: bool) -> None:
        self.drawer = value
        m = S.lg if value else 0
        self.layout_.setContentsMargins(m, m, 0 if value else 0, m)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self.drawer:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(qcolor(C.border, BORDER_STRONG_A), 1))
        p.setBrush(QColor(C.bg))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, R.xl + 2, -0.5), R.xl, R.xl)


class _Scrim(QWidget):
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.opacity = 0.0

    def paintEvent(self, event) -> None:  # noqa: N802
        if self.opacity > 0:
            QPainter(self).fillRect(self.rect(), QColor(4, 8, 18, round(150 * self.opacity)))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()


class SplitBody(QWidget):
    """List | detail split on wide windows; below a width the detail becomes a slide-over drawer."""

    drawer_closed = Signal()

    def __init__(self, primary: QWidget, secondary: QWidget, parent: QWidget | None = None):
        super().__init__(parent)
        self.primary = primary
        self.host = _DrawerSurface(self)
        self.host.layout_.addWidget(secondary)
        self.secondary = secondary
        primary.setParent(self)
        self.scrim = _Scrim(self)
        self.scrim.hide()
        self.scrim.clicked.connect(self.close_drawer)
        self._mode = "split"
        self._progress = 1.0
        self._open = False
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(M.panel)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_progress)
        self._anim.finished.connect(self._anim_done)
        self.split_gap = S.xl
        self.host.raise_()

    def mode(self) -> str:
        return self._mode

    def drawer_open(self) -> bool:
        return self._mode == "drawer" and self._open

    def set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self._open = False
        self._progress = 1.0 if mode == "split" else 0.0
        self.host.set_drawer(mode == "drawer")
        self.scrim.hide()
        self._apply()

    def open_drawer(self) -> None:
        if self._mode != "drawer" or self._open:
            return
        self._open = True
        self._animate(1.0)

    def close_drawer(self) -> None:
        if self._mode != "drawer" or not self._open:
            return
        self._open = False
        self._animate(0.0)
        self.drawer_closed.emit()

    def _animate(self, target: float) -> None:
        self._anim.stop()
        self.host.show()
        self._anim.setStartValue(self._progress)
        self._anim.setEndValue(target)
        self._anim.start()

    def _set_progress(self, value) -> None:
        self._progress = float(value)
        self._apply()

    def _anim_done(self) -> None:
        if self._mode == "drawer" and not self._open:
            self.host.hide()
            self.scrim.hide()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        w, h = self.width(), self.height()
        if self._mode == "split":
            list_w = max(340, min(round(w * 0.40), w - self.split_gap - 340))
            self.primary.setGeometry(0, 0, list_w, h)
            self.host.setGeometry(list_w + self.split_gap, 0, max(0, w - list_w - self.split_gap), h)
            self.host.show()
            self.scrim.hide()
            return
        self.primary.setGeometry(0, 0, w, h)
        drawer_w = min(480, max(300, w - S.xl))
        x = round(w - drawer_w * self._progress) if self._progress > 0 else w
        self.host.setGeometry(x, 0, drawer_w + R.xl + 2, h)
        self.host.setVisible(self._progress > 0 or self._open)
        self.scrim.setGeometry(0, 0, w, h)
        self.scrim.opacity = self._progress
        self.scrim.setVisible(self._progress > 0)
        self.scrim.raise_()
        self.host.raise_()
        self.scrim.update()
