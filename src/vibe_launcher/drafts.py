"""Unsaved form input kept on disk, so a dialog that closes early or a save that fails does not lose what was typed.

Only plain fields are kept: password-mode inputs and fields whose name looks like a secret are never written.
Drafts live in ``<data>/drafts`` (private files) and expire after a week.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QPlainTextEdit, QWidget

from . import runtime

log = logging.getLogger(__name__)
DRAFT_TTL = 7 * 24 * 3600
SAVE_DELAY_MS = 400
MAX_TEXT = 65536
SECRETISH = ("password", "passwd", "secret", "token", "api_key", "apikey", "otp")


def draft_path(root: Path, key: str) -> Path:
    return root / "drafts" / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".json")


def _keepable(name: str, widget: QWidget) -> bool:
    if any(word in name.lower() for word in SECRETISH):
        return False
    return not isinstance(widget, QLineEdit) or widget.echoMode() == QLineEdit.EchoMode.Normal


def _read(widget: QWidget):
    if isinstance(widget, QLineEdit):
        return widget.text()
    if isinstance(widget, QPlainTextEdit):
        return widget.toPlainText()
    if isinstance(widget, QComboBox):
        return widget.currentData()
    if isinstance(widget, QCheckBox):
        return widget.isChecked()
    return None


def _write(widget: QWidget, value) -> None:
    if isinstance(widget, QLineEdit) and isinstance(value, str):
        widget.setText(value)
    elif isinstance(widget, QPlainTextEdit) and isinstance(value, str):
        widget.setPlainText(value)
    elif isinstance(widget, QComboBox):
        index = widget.findData(value)
        if index >= 0:
            widget.setCurrentIndex(index)
    elif isinstance(widget, QCheckBox) and isinstance(value, bool):
        widget.setChecked(value)


class FormDraft(QObject):
    """Mirror ``fields`` into a draft file while the user edits; ``restored`` says whether an older draft came back."""

    def __init__(self, owner: QWidget, root: Path, key: str, fields: dict[str, QWidget]):
        super().__init__(owner)
        self.path = draft_path(root, key)
        self.fields = {name: widget for name, widget in fields.items() if _keepable(name, widget)}
        self._initial = self.snapshot()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SAVE_DELAY_MS)
        self._timer.timeout.connect(self.save)
        self.restored = self._restore()
        for widget in self.fields.values():
            signal = (widget.textChanged if isinstance(widget, (QLineEdit, QPlainTextEdit)) else
                      widget.currentIndexChanged if isinstance(widget, QComboBox) else
                      widget.toggled if isinstance(widget, QCheckBox) else None)
            if signal is not None:
                signal.connect(self._timer.start)

    def snapshot(self) -> dict:
        values = {name: _read(widget) for name, widget in self.fields.items()}
        return {name: value for name, value in values.items()
                if isinstance(value, bool) or (isinstance(value, str) and len(value) <= MAX_TEXT)}

    def _restore(self) -> bool:
        try:
            if not self.path.exists():
                return False
            data = json.loads(runtime.read_private(self.path, limit=4 * MAX_TEXT))
            if not isinstance(data, dict) or time.time() - float(data.get("saved", 0)) > DRAFT_TTL:
                self.clear()
                return False
            values = data.get("values")
            if not isinstance(values, dict) or values == self._initial:
                return False
            # Combo boxes first: a choice can change what the text fields next to it mean.
            for name in sorted(values, key=lambda n: not isinstance(self.fields.get(n), QComboBox)):
                if name in self.fields:
                    _write(self.fields[name], values[name])
            return True
        except (OSError, ValueError, TypeError) as error:
            log.warning("draft restore failed: %s", type(error).__name__)
            return False

    def save(self) -> None:
        self._timer.stop()
        values = self.snapshot()
        try:
            if values == self._initial:
                self.path.unlink(missing_ok=True)   # back to what the form started with: nothing to keep
            else:
                runtime.write_json(self.path, {"saved": time.time(), "values": values})
        except (OSError, ValueError) as error:
            log.warning("draft save failed: %s", type(error).__name__)

    def clear(self) -> None:
        """Forget the draft once its content was saved for real (or the user discarded it)."""
        self._timer.stop()
        try:
            self.path.unlink(missing_ok=True)
        except OSError as error:
            log.warning("draft clear failed: %s", type(error).__name__)


def clear_draft(root: Path, key: str) -> None:
    try:
        draft_path(root, key).unlink(missing_ok=True)
    except OSError as error:
        log.warning("draft clear failed: %s", type(error).__name__)
