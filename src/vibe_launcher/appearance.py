"""外觀: pick a colour theme, an accent colour and terminal preferences, previewed live.

Every change is applied to the whole window at once (``changed``), so the dialog itself is the preview. Cancel puts
back what was there when it opened; Done keeps it.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QCheckBox, QColorDialog, QDialog, QFrame, QGraphicsDropShadowEffect, QGridLayout,
    QHBoxLayout, QLabel, QSpinBox, QVBoxLayout, QWidget,
)

from .kit import Hover, KbFocus, draw_focus_ring, draw_icon, make_button
from .theme import (
    ACCENT_PRESETS, TERMINAL_FONT_SIZES, THEMES, C, R, S, T, Theme, contrast, font, qcolor, valid_color,
)


class ThemeCard(KbFocus, Hover, QAbstractButton):
    """A miniature of a theme: its sidebar, a card, the accent and the terminal colours, drawn in its own palette."""

    def __init__(self, theme: Theme, parent: QWidget | None = None):
        super().__init__(parent)
        self._init_hover()
        self.theme = theme
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"主題 {theme.name}")
        self.setToolTip(theme.name + ("（淺色）" if not theme.dark else ""))
        self.setFixedSize(QSize(168, 104))

    def paintEvent(self, event) -> None:  # noqa: N802
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        outer = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t.bg))
        p.drawRoundedRect(outer, R.lg, R.lg)
        p.save()
        p.setClipRect(outer.adjusted(0, 0, -outer.width() + 34, 0))
        p.setBrush(QColor(t.sidebar))
        p.drawRoundedRect(outer, R.lg, R.lg)
        p.restore()
        for i in range(3):                                   # sidebar items, the first one "selected"
            p.setBrush(qcolor(t.accent if i == 0 else t.text_muted, 0.9 if i == 0 else 0.45))
            p.drawRoundedRect(QRectF(outer.left() + 9, outer.top() + 12 + i * 12, 16, 5), 2.5, 2.5)
        card = QRectF(outer.left() + 44, outer.top() + 10, outer.width() - 54, 46)
        p.setBrush(QColor(t.surface))
        p.drawRoundedRect(card, R.md, R.md)
        p.setBrush(QColor(t.text))
        p.drawRoundedRect(QRectF(card.left() + 8, card.top() + 9, 54, 5), 2.5, 2.5)
        p.setBrush(QColor(t.text_muted))
        p.drawRoundedRect(QRectF(card.left() + 8, card.top() + 20, 74, 4), 2, 2)
        p.setBrush(QColor(t.accent))
        p.drawRoundedRect(QRectF(card.right() - 34, card.bottom() - 15, 26, 9), 4.5, 4.5)
        for i, color in enumerate(t.ansi[1:7]):              # red .. cyan, the colours programs print in
            p.setBrush(QColor(color))
            p.drawEllipse(QRectF(outer.left() + 44 + i * 13, outer.bottom() - 36, 9, 9))
        p.setFont(font(T.meta, QFont.Weight.DemiBold))
        p.setPen(QColor(t.text))
        p.drawText(QRectF(outer.left() + 44, outer.bottom() - 22, outer.width() - 50, 18),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, t.name)
        if self.isChecked() or self._hv > 0:
            pen_color = qcolor(C.accent, 1.0) if self.isChecked() else qcolor(C.border, 0.45 * self._hv)
            p.setPen(pen_color)
            p.setBrush(Qt.BrushStyle.NoBrush)
            pen = p.pen()
            pen.setWidthF(2.0 if self.isChecked() else 1.0)
            p.setPen(pen)
            p.drawRoundedRect(outer.adjusted(-1, -1, 1, 1), R.lg + 1, R.lg + 1)
        else:
            p.setPen(qcolor(C.border, 0.22))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(outer, R.lg, R.lg)
        if self.isChecked():
            badge = QRectF(outer.right() - 22, outer.top() + 6, 16, 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C.accent))
            p.drawEllipse(badge)
            draw_icon(p, self, "check", C.accent_ink, badge.left() + 3, badge.top() + 3, 10)
        if self._kb:
            draw_focus_ring(p, QRectF(self.rect()), R.lg + 2)


class Swatch(KbFocus, Hover, QAbstractButton):
    """A round accent colour choice."""

    def __init__(self, color: str, tip: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._init_hover()
        self.color = color
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.setFixedSize(30, 30)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        if self.isChecked():
            p.setPen(qcolor(C.text, 0.9))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(self.rect()).adjusted(1, 1, -1, -1))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(self.color))
        grow = 1.5 * self._hv
        p.drawEllipse(r.adjusted(2 - grow, 2 - grow, -2 + grow, -2 + grow))
        if self._kb:
            draw_focus_ring(p, QRectF(self.rect()), 15)


class AppearanceDialog(QDialog):
    """Theme, accent colour and terminal settings. ``changed`` carries the full settings on every edit."""

    changed = Signal(dict)

    def __init__(self, parent: QWidget | None, settings: dict[str, Any]):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setWindowTitle("外觀")
        self.original = dict(settings)
        self.settings = dict(settings)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 20, 28, 36)
        panel = QFrame()
        panel.setObjectName("palettePanel")
        shadow = QGraphicsDropShadowEffect(panel)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 12)
        shadow.setColor(QColor(0, 0, 0, 150))
        panel.setGraphicsEffect(shadow)
        outer.addWidget(panel)
        body = QVBoxLayout(panel)
        body.setContentsMargins(S.xl, S.xl, S.xl, S.lg + 2)
        body.setSpacing(S.md)

        heading = QLabel("外觀")
        heading.setProperty("role", "section")
        heading.setFont(font(T.dialog_title, QFont.Weight.DemiBold))
        body.addWidget(heading)
        hint = QLabel("選好立即套用到整個視窗與所有終端，可以直接預覽。")
        hint.setProperty("role", "muted")
        body.addWidget(hint)

        body.addWidget(self._section("主題"))
        grid = QGridLayout()
        grid.setSpacing(S.sm)
        self.theme_group = QButtonGroup(self)
        self.cards: dict[str, ThemeCard] = {}
        for index, theme in enumerate(THEMES.values()):
            card = ThemeCard(theme)
            card.setChecked(theme.id == self.settings["theme"])
            card.clicked.connect(lambda _=False, i=theme.id: self._set(theme=i))
            self.theme_group.addButton(card)
            self.cards[theme.id] = card
            grid.addWidget(card, index // 4, index % 4)
        body.addLayout(grid)

        body.addWidget(self._section("強調色"))
        row = QHBoxLayout()
        row.setSpacing(S.xs)
        self.accent_group = QButtonGroup(self)
        self.theme_swatch = Swatch(C.accent, "使用主題預設的強調色")
        self.theme_swatch.clicked.connect(lambda: self._set(accent=""))
        self.accent_group.addButton(self.theme_swatch)
        row.addWidget(self.theme_swatch)
        default_label = QLabel("主題預設")
        default_label.setProperty("role", "meta")
        row.addWidget(default_label)
        row.addSpacing(S.md)
        self.swatches: list[Swatch] = []
        for color in ACCENT_PRESETS:
            swatch = Swatch(color, f"強調色 {color}")
            swatch.clicked.connect(lambda _=False, c=color: self._set(accent=c))
            self.accent_group.addButton(swatch)
            self.swatches.append(swatch)
            row.addWidget(swatch)
        self.custom_swatch = Swatch(C.accent, "自訂的強調色")
        self.custom_swatch.clicked.connect(lambda: self._set(accent=self.custom_swatch.color))
        self.accent_group.addButton(self.custom_swatch)
        row.addWidget(self.custom_swatch)
        self.custom_button = make_button("自訂…", "ghost", "palette", small=True, callback=self._pick_custom)
        row.addWidget(self.custom_button)
        row.addStretch(1)
        body.addLayout(row)
        self.contrast_note = QLabel("")
        self.contrast_note.setProperty("role", "meta")
        self.contrast_note.setWordWrap(True)
        body.addWidget(self.contrast_note)

        body.addWidget(self._section("終端與輸入"))
        size_row = QHBoxLayout()
        size_row.setSpacing(S.sm)
        size_label = QLabel("終端字級")
        size_label.setProperty("role", "muted")
        size_row.addWidget(size_label)
        self.font_size = QSpinBox()
        self.font_size.setRange(TERMINAL_FONT_SIZES.start, TERMINAL_FONT_SIZES.stop - 1)
        self.font_size.setSuffix(" px")
        self.font_size.setValue(int(self.settings["terminal_font_size"]))
        self.font_size.setAccessibleName("終端字級")
        self.font_size.valueChanged.connect(lambda value: self._set(terminal_font_size=int(value)))
        size_row.addWidget(self.font_size)
        size_row.addStretch(1)
        body.addLayout(size_row)
        self.cursor_blink = QCheckBox("游標閃爍")
        self.cursor_blink.setChecked(bool(self.settings["cursor_blink"]))
        self.cursor_blink.toggled.connect(lambda on: self._set(cursor_blink=bool(on)))
        body.addWidget(self.cursor_blink)
        self.composer = QCheckBox("開啟 Codex／Claude 終端時顯示輸入列（中文輸入更順手，Ctrl+Shift+E 切換）")
        self.composer.setChecked(bool(self.settings["composer_default"]))
        self.composer.toggled.connect(lambda on: self._set(composer_default=bool(on)))
        body.addWidget(self.composer)

        buttons = QHBoxLayout()
        buttons.setSpacing(S.sm)
        reset = make_button("還原預設", "ghost", "refresh", callback=self._reset)
        buttons.addWidget(reset)
        buttons.addStretch(1)
        self.cancel_button = make_button("取消", "ghost", callback=self.reject)
        self.done_button = make_button("完成", "primary", callback=self.accept)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.done_button)
        body.addSpacing(S.sm)
        body.addLayout(buttons)
        self._sync_accent()
        self.done_button.setFocus()

    @staticmethod
    def _section(text: str) -> QLabel:
        label = QLabel(text)
        label.setFont(font(T.meta, QFont.Weight.DemiBold))
        label.setProperty("role", "muted")
        return label

    # -- edits --------------------------------------------------------------------------------------
    def _set(self, **values: Any) -> None:
        self.settings.update(values)
        self.changed.emit(dict(self.settings))
        self._sync_accent()

    def _pick_custom(self) -> None:
        color = QColorDialog.getColor(QColor(self.settings.get("accent") or C.accent), self, "自訂強調色")
        if color.isValid():
            self._set(accent=color.name().upper())

    def _reset(self) -> None:
        defaults = {"theme": "deep-sea", "accent": "", "terminal_font_size": 14, "cursor_blink": False,
                    "composer_default": True}
        for widget, value in ((self.font_size, 14), (self.cursor_blink, False), (self.composer, True)):
            widget.blockSignals(True)
            widget.setValue(value) if isinstance(widget, QSpinBox) else widget.setChecked(value)
            widget.blockSignals(False)
        self.cards["deep-sea"].setChecked(True)
        self._set(**defaults)

    def _sync_accent(self) -> None:
        """Mark the chosen accent, keep the 'theme default' dot in the theme's colour, warn about weak contrast."""
        theme = THEMES.get(self.settings["theme"]) or THEMES["deep-sea"]
        self.theme_swatch.color = theme.accent
        accent = valid_color(self.settings.get("accent"))
        presets = {c.upper(): s for c, s in zip(ACCENT_PRESETS, self.swatches)}
        if not accent:
            self.theme_swatch.setChecked(True)
        elif accent in presets:
            presets[accent].setChecked(True)
        else:
            self.custom_swatch.color = accent
            self.custom_swatch.setChecked(True)
        self.custom_swatch.setVisible(bool(accent) and accent not in presets)
        ratio = contrast(accent or theme.accent, theme.bg)
        self.contrast_note.setText("" if ratio >= 3 else
                                   f"這個強調色在「{theme.name}」背景上的對比只有 {ratio:.1f}:1，按鈕與連結可能不易辨識。")
        self.contrast_note.setVisible(ratio < 3)
        for widget in (self.theme_swatch, self.custom_swatch, *self.swatches):
            widget.update()

    def reject(self) -> None:
        if self.settings != self.original:
            self.changed.emit(dict(self.original))
        super().reject()

    @staticmethod
    def ask(parent: QWidget, settings: dict[str, Any], apply) -> dict[str, Any] | None:
        """Show the dialog, applying every change through ``apply(settings)``. Returns the kept settings, or None."""
        dialog = AppearanceDialog(parent, settings)
        dialog.changed.connect(apply)
        dialog.adjustSize()
        center = parent.window().geometry().center()
        dialog.move(center.x() - dialog.width() // 2, center.y() - dialog.height() // 2)
        return dialog.settings if dialog.exec() == QDialog.DialogCode.Accepted else None
