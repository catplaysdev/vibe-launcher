"""外觀: pick a colour theme, an accent colour and terminal preferences, previewed live.

Every change is applied to the whole window at once (``changed``), so the dialog itself is the preview. Cancel puts
back what was there when it opened; Done keeps it.
"""

from __future__ import annotations

from .i18n import tr

from typing import Any

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QCheckBox, QColorDialog, QDialog, QFrame, QGraphicsDropShadowEffect, QGridLayout,
    QAbstractItemView, QApplication, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QSpinBox,
    QVBoxLayout, QWidget,
)

from .kit import FlowLayout, Hover, KbFocus, draw_focus_ring, draw_icon, make_button
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
        self.setAccessibleName(tr('appearance.ad674722c39e', v0=f'{theme.name}'))
        self.setToolTip(theme.name + (tr('appearance.33af3a38f7f8') if not theme.dark else ""))
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


class SearchableCombo(QComboBox):
    """A drop-down whose list opens with a search field on top: type to narrow a long list (e.g. every installed font)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._popup: QFrame | None = None

    def showPopup(self) -> None:  # noqa: N802
        if self._popup is not None or not self.count():
            return
        popup = QFrame(self, Qt.WindowType.Popup)
        popup.setObjectName("palettePanel")
        layout = QVBoxLayout(popup)
        layout.setContentsMargins(S.sm, S.sm, S.sm, S.sm)
        layout.setSpacing(S.xs)
        search = QLineEdit()
        search.setPlaceholderText(tr("Search…"))
        search.setClearButtonEnabled(True)
        search.setAccessibleName(tr("Search"))
        listing = QListWidget()
        listing.setUniformItemSizes(True)
        for row in range(self.count()):
            item = QListWidgetItem(self.itemText(row))
            item.setData(Qt.ItemDataRole.UserRole, row)
            listing.addItem(item)
        layout.addWidget(search)
        layout.addWidget(listing)

        def narrow(text: str) -> None:
            needle = text.strip().casefold()
            first = None
            for row in range(listing.count()):
                item = listing.item(row)
                hidden = bool(needle) and needle not in item.text().casefold()
                item.setHidden(hidden)
                if first is None and not hidden:
                    first = item
            if first is not None:
                listing.setCurrentItem(first)

        def choose(item: QListWidgetItem | None = None) -> None:
            item = item or listing.currentItem()
            if item is not None and not item.isHidden():
                self.setCurrentIndex(int(item.data(Qt.ItemDataRole.UserRole)))
            popup.close()

        def key(event) -> None:
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                QApplication.sendEvent(listing, event)          # arrows move through the results while typing
            elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                choose()
            else:
                QLineEdit.keyPressEvent(search, event)

        search.keyPressEvent = key
        search.textChanged.connect(narrow)
        listing.itemClicked.connect(choose)
        popup.destroyed.connect(lambda: setattr(self, "_popup", None))
        self._popup = popup
        popup.setFixedWidth(max(self.width(), 280))
        popup.setFixedHeight(min(340, 64 + 28 * self.count()))
        popup.move(self.mapToGlobal(self.rect().bottomLeft()))
        current = listing.item(max(self.currentIndex(), 0))
        listing.setCurrentItem(current)
        listing.scrollToItem(current, QAbstractItemView.ScrollHint.PositionAtCenter)
        popup.show()
        search.setFocus()

    def hidePopup(self) -> None:  # noqa: N802
        if self._popup is not None:
            self._popup.close()


class AppearancePanel(QWidget):
    """Theme, accent colour and terminal choices. ``changed`` carries the full settings on every edit.

    Used inside Settings (the window itself is the live preview) and by the stand-alone ``AppearanceDialog``.
    """

    changed = Signal(dict)

    def __init__(self, settings: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = dict(settings)
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(S.md)

        body.addWidget(self._section(tr('appearance.e9666224fbb7')))
        cards_host = QWidget()
        grid = FlowLayout(cards_host, S.sm, S.sm)      # as many per row as fit
        grid.setContentsMargins(0, 0, 0, 0)
        self.theme_group = QButtonGroup(self)
        self.cards: dict[str, ThemeCard] = {}
        for index, theme in enumerate(THEMES.values()):
            card = ThemeCard(theme)
            card.setChecked(theme.id == self.settings["theme"])
            card.clicked.connect(lambda _=False, i=theme.id: self._set(theme=i))
            self.theme_group.addButton(card)
            self.cards[theme.id] = card
            grid.addWidget(card)
        body.addWidget(cards_host)

        body.addWidget(self._section(tr('appearance.74fd117e8992')))
        swatches_host = QWidget()
        row = FlowLayout(swatches_host, S.xs, S.xs)
        row.setContentsMargins(0, 0, 0, 0)
        self.accent_group = QButtonGroup(self)
        self.theme_swatch = Swatch(C.accent, tr('appearance.c509bbc8d721'))
        self.theme_swatch.clicked.connect(lambda: self._set(accent=""))
        self.accent_group.addButton(self.theme_swatch)
        row.addWidget(self.theme_swatch)
        default_label = QLabel(tr('appearance.8b7fe717399d'))
        default_label.setProperty("role", "meta")
        row.addWidget(default_label)
        self.swatches: list[Swatch] = []
        for color in ACCENT_PRESETS:
            swatch = Swatch(color, tr('appearance.ff91a332336a', v0=f'{color}'))
            swatch.clicked.connect(lambda _=False, c=color: self._set(accent=c))
            self.accent_group.addButton(swatch)
            self.swatches.append(swatch)
            row.addWidget(swatch)
        self.custom_swatch = Swatch(C.accent, tr('appearance.8e10ad056ff7'))
        self.custom_swatch.clicked.connect(lambda: self._set(accent=self.custom_swatch.color))
        self.accent_group.addButton(self.custom_swatch)
        row.addWidget(self.custom_swatch)
        self.custom_button = make_button(tr('appearance.a393592d7f00'), "ghost", "palette", small=True, callback=self._pick_custom)
        row.addWidget(self.custom_button)
        body.addWidget(swatches_host)
        self.contrast_note = QLabel("")
        self.contrast_note.setProperty("role", "meta")
        self.contrast_note.setWordWrap(True)
        body.addWidget(self.contrast_note)

        body.addWidget(self._section(tr("Fonts")))
        self.ui_font = self._font_row(body, tr("Interface font"), "ui_font", mono=False)
        self.terminal_font = self._font_row(body, tr("Terminal font"), "terminal_font", mono=True)

        body.addWidget(self._section(tr('appearance.0c20746755c5')))
        size_row = QHBoxLayout()
        size_row.setSpacing(S.sm)
        size_label = QLabel(tr('appearance.3f94fa415a75'))
        size_label.setProperty("role", "muted")
        size_row.addWidget(size_label)
        self.font_size = QSpinBox()
        self.font_size.setRange(TERMINAL_FONT_SIZES.start, TERMINAL_FONT_SIZES.stop - 1)
        self.font_size.setSuffix(" px")
        self.font_size.setValue(int(self.settings["terminal_font_size"]))
        self.font_size.setAccessibleName(tr('appearance.3f94fa415a75'))
        self.font_size.valueChanged.connect(lambda value: self._set(terminal_font_size=int(value)))
        size_row.addWidget(self.font_size)
        size_row.addStretch(1)
        body.addLayout(size_row)
        self.cursor_blink = QCheckBox(tr('appearance.a3bf11efe3f5'))
        self.cursor_blink.setChecked(bool(self.settings["cursor_blink"]))
        self.cursor_blink.toggled.connect(lambda on: self._set(cursor_blink=bool(on)))
        body.addWidget(self.cursor_blink)
        self.composer = QCheckBox(tr('appearance.84797e038ebb'))
        self.composer.setChecked(bool(self.settings["composer_default"]))
        self.composer.toggled.connect(lambda on: self._set(composer_default=bool(on)))
        body.addWidget(self.composer)
        self.reset_button = make_button(tr('appearance.b7b6b64b8404'), "ghost", "refresh", small=True, callback=self._reset)
        body.addWidget(self.reset_button, 0, Qt.AlignmentFlag.AlignLeft)
        self._sync_accent()

    def _font_row(self, body: QVBoxLayout, text: str, key: str, *, mono: bool) -> QComboBox:
        """A drop-down of the installed families (fixed-pitch ones for the terminal); the first entry keeps the default."""
        row = QHBoxLayout()
        row.setSpacing(S.sm)
        label = QLabel(text)
        label.setProperty("role", "muted")
        label.setMinimumWidth(96)                    # the two drop-downs line up
        row.addWidget(label)
        combo = SearchableCombo()
        combo.setMinimumContentsLength(24)
        combo.setMaxVisibleItems(14)
        combo.setAccessibleName(text)
        names = [f for f in QFontDatabase.families() if not f.startswith("@") and (not mono or QFontDatabase.isFixedPitch(f))]
        current = str(self.settings.get(key, ""))
        if current and current not in names:
            names.append(current)                    # a font that is no longer installed stays visible, not silently dropped
        combo.addItem(tr("Default"), "")
        for name in sorted(set(names), key=str.casefold):
            combo.addItem(name, name)
        combo.setCurrentIndex(max(combo.findData(current), 0))
        combo.currentIndexChanged.connect(lambda _=0, c=combo, k=key: self._set(**{k: c.currentData() or ""}))
        row.addWidget(combo, 1)
        body.addLayout(row)
        return combo

    @staticmethod
    def _section(text: str) -> QLabel:
        label = QLabel(text)
        label.setProperty("role", "subsection")
        return label

    # -- edits --------------------------------------------------------------------------------------
    def _set(self, **values: Any) -> None:
        self.settings.update(values)
        self.changed.emit(dict(self.settings))
        self._sync_accent()

    def _pick_custom(self) -> None:
        color = QColorDialog.getColor(QColor(self.settings.get("accent") or C.accent), self, tr('appearance.f91fa7d21871'))
        if color.isValid():
            self._set(accent=color.name().upper())

    def _reset(self) -> None:
        defaults = {"theme": "deep-sea", "accent": "", "terminal_font_size": 14, "cursor_blink": False,
                    "composer_default": True, "ui_font": "", "terminal_font": ""}
        for combo in (self.ui_font, self.terminal_font):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
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
                                   tr('appearance.734357ec31cf', v0=f'{theme.name}', v1=f'{ratio:.1f}'))
        self.contrast_note.setVisible(ratio < 3)
        for widget in (self.theme_swatch, self.custom_swatch, *self.swatches):
            widget.update()


class AppearanceDialog(QDialog):
    """The appearance panel on its own (command palette). Cancel puts back what was there when it opened."""

    changed = Signal(dict)

    def __init__(self, parent: QWidget | None, settings: dict[str, Any]):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setWindowTitle(tr('appearance.b70682d027ce'))
        self.original = dict(settings)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 20, 28, 36)
        frame = QFrame()
        frame.setObjectName("palettePanel")
        shadow = QGraphicsDropShadowEffect(frame)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 12)
        shadow.setColor(QColor(0, 0, 0, 150))
        frame.setGraphicsEffect(shadow)
        outer.addWidget(frame)
        body = QVBoxLayout(frame)
        body.setContentsMargins(S.xl, S.xl, S.xl, S.lg + 2)
        body.setSpacing(S.md)
        heading = QLabel(tr('appearance.b70682d027ce'))
        heading.setProperty("role", "dialogTitle")
        body.addWidget(heading)
        hint = QLabel(tr('appearance.9fefa9e19f34'))
        hint.setProperty("role", "muted")
        body.addWidget(hint)
        self.panel = AppearancePanel(settings)
        self.panel.changed.connect(self.changed)
        body.addWidget(self.panel)
        # Kept as attributes so callers (and tests) can drive the controls directly.
        self.cards, self.swatches, self.font_size = self.panel.cards, self.panel.swatches, self.panel.font_size
        self.ui_font, self.terminal_font = self.panel.ui_font, self.panel.terminal_font
        self.cursor_blink, self.composer = self.panel.cursor_blink, self.panel.composer

        buttons = QHBoxLayout()
        buttons.setSpacing(S.sm)
        buttons.addStretch(1)
        self.cancel_button = make_button(tr('appearance.2cd0f3be8738'), "ghost", callback=self.reject)
        self.done_button = make_button(tr('appearance.c0b3fbff51cc'), "primary", callback=self.accept)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.done_button)
        body.addSpacing(S.sm)
        body.addLayout(buttons)
        self.done_button.setFocus()

    @property
    def settings(self) -> dict[str, Any]:
        return self.panel.settings

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
