"""輸入列: a native text box under a terminal, for writing to Codex / Claude comfortably.

Typing Chinese straight into a full-screen terminal program is awkward: the program draws its own input box, so the
input method's candidate window follows the terminal's real cursor, which is somewhere else. Here the text is edited
in an ordinary Qt editor (the candidate window sits right at the caret, the text can be revised freely) and handed to
the program only on Enter: one line as typed, several lines as one paste.

Keys that steer the program still reach it from here: Esc and Ctrl+C always, and with an empty box ↑ ↓ Tab Shift+Tab
and Enter, so menus and confirmations work without leaving the box.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QKeyEvent, QTextOption
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPlainTextEdit, QWidget

from .kit import IconButton
from .theme import S, T, font

MAX_LINES = 6
HISTORY = 50

# Keys passed to the program while the box is empty (navigation in its menus, accepting a default).
_EMPTY_KEYS = {
    (Qt.Key.Key_Up, False): b"\x1b[A",
    (Qt.Key.Key_Down, False): b"\x1b[B",
    (Qt.Key.Key_Tab, False): b"\t",
    (Qt.Key.Key_Backtab, True): b"\x1b[Z",
    (Qt.Key.Key_Tab, True): b"\x1b[Z",
}


def chord_for(event: QKeyEvent) -> str | None:
    """The workspace chords (same as in the terminal page) so they work from the input bar too."""
    mods = event.modifiers()
    ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
    shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
    alt = bool(mods & Qt.KeyboardModifier.AltModifier)
    key = event.key()
    if ctrl and not alt and key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
        return "prev" if shift or key == Qt.Key.Key_Backtab else "next"
    if alt and not ctrl and not shift and Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
        return f"goto:{key - Qt.Key.Key_0}"
    if ctrl and shift and not alt:
        return {Qt.Key.Key_K: "palette", Qt.Key.Key_D: "detach", Qt.Key.Key_W: "close", Qt.Key.Key_T: "new",
                Qt.Key.Key_E: "composer"}.get(key)
    return None


class ComposerEdit(QPlainTextEdit):
    """The editor. Grows from one to MAX_LINES lines, then scrolls."""

    submitted = Signal(str)
    raw = Signal(bytes)
    chord = Signal(str)
    focused = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("composerEdit")
        self.setFont(font(T.body + 1))
        self.document().setDocumentMargin(6)
        self.setTabChangesFocus(False)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setPlaceholderText("輸入給程式的文字… Enter 送出 · Shift+Enter 換行 · Ctrl+↑ 上一則 · Esc 傳給程式")
        self.history: list[str] = []
        self._recall = 0                       # 0 = not browsing; n = n-th most recent entry
        self._draft = ""
        self.document().contentsChanged.connect(self._fit)
        self._fit()

    def _line_height(self) -> int:
        return QFontMetrics(self.font()).lineSpacing()

    def _fit(self) -> None:
        lines = max(1, min(MAX_LINES, round(self.document().size().height())))
        margins = self.contentsMargins()
        doc_margin = round(self.document().documentMargin()) * 2
        self.setFixedHeight(lines * self._line_height() + doc_margin + margins.top() + margins.bottom() + 6)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(200, self.height())

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.focused.emit()

    def send(self) -> None:
        text = self.toPlainText().replace("\u2029", "\n").replace("\u2028", "\n")
        if not text.strip():
            self.clear()
            self.raw.emit(b"\r")
            return
        if not self.history or self.history[-1] != text:
            self.history.append(text)
            del self.history[:-HISTORY]
        self._recall, self._draft = 0, ""
        self.clear()
        self.submitted.emit(text)

    def recall(self, step: int) -> None:
        """Ctrl+↑ / Ctrl+↓: walk through what was sent from this box."""
        if not self.history:
            return
        if self._recall == 0:
            self._draft = self.toPlainText()
        self._recall = max(0, min(len(self.history), self._recall + step))
        self.setPlainText(self._draft if self._recall == 0 else self.history[-self._recall])
        self.moveCursor(self.textCursor().MoveOperation.End)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        key = event.key()
        chord = chord_for(event)
        if chord:
            self.chord.emit(chord)
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if shift:
                self.insertPlainText("\n")
            elif not ctrl:
                self.send()
            return
        if key == Qt.Key.Key_Escape:
            self.raw.emit(b"\x1b")
            return
        if ctrl and not shift and key == Qt.Key.Key_C and not self.textCursor().hasSelection():
            self.raw.emit(b"\x03")
            return
        if ctrl and key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.recall(1 if key == Qt.Key.Key_Up else -1)
            return
        if not self.toPlainText() and not ctrl:
            data = _EMPTY_KEYS.get((key, shift))
            if data is not None:
                self.raw.emit(data)
                return
        super().keyPressEvent(event)


class ComposerBar(QFrame):
    """The editor plus a send button, styled by the application stylesheet (QFrame#composer)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("composer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(S.sm, S.sm, S.sm, S.sm)
        row.setSpacing(S.sm)
        self.edit = ComposerEdit()
        row.addWidget(self.edit, 1)
        self.send_button = IconButton("send", "送出（Enter）", size=32, icon_size=16)
        self.send_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.send_button.clicked.connect(self.edit.send)
        row.addWidget(self.send_button, 0, Qt.AlignmentFlag.AlignBottom)
        # Re-exported so the pane only deals with the bar.
        self.submitted, self.raw, self.chord, self.focused = (self.edit.submitted, self.edit.raw, self.edit.chord,
                                                              self.edit.focused)

    def focus(self) -> None:
        self.edit.setFocus(Qt.FocusReason.OtherFocusReason)
