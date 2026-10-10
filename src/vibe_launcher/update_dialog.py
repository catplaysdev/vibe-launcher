"""Update window: shows what the update helper is doing (steps, download size, what changes) until the launcher restarts.

The helper (``updates apply`` / ``apply-release``) runs as a hidden process and mirrors its progress into
``update-progress.json``. This window only displays that file; whether the update worked is the helper's exit code.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QDialog, QHBoxLayout, QProgressBar, QScrollArea, QVBoxLayout, QWidget

from . import runtime, updates
from .i18n import tr
from .kit import label, later, make_button, set_tone
from .theme import C, S

POLL_MS = 300
STALL_SECONDS = 120            # the installer normally needs well under a minute after the launcher closes
MARK = {"todo": "○", "run": "●", "ok": "✓", "fail": "✕"}
TONE = {"todo": "muted", "run": "accent", "ok": "ok", "fail": "danger"}


def megabytes(count: int) -> str:
    return f"{count / (1024 * 1024):.1f} MB"


class UpdateDialog(QDialog):
    launched = Signal()        # the installer has started: it is about to close the launcher

    def __init__(self, args: list[str], data_root: Path, releases: list[dict[str, Any]], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("Update Vibe Launcher"))
        self.setMinimumWidth(520)
        self.setModal(False)                      # a modal window would make Qt ignore the installer's close request
        self._args = args
        self._data = Path(data_root)
        self._releases = releases
        self._process: subprocess.Popen | None = None
        self._state = "running"
        self._launched_at: float | None = None
        self._last: dict[str, Any] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(S.xl, S.xl, S.xl, S.lg)
        root.setSpacing(S.md)
        root.addWidget(label(tr("Update Vibe Launcher"), "dialogTitle"))
        self.versions = label("", "subsection")
        root.addWidget(self.versions)
        self.step_labels: list[Any] = []
        self.steps = QVBoxLayout()
        self.steps.setSpacing(S.xs)
        root.addLayout(self.steps)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        self.bar.setRange(0, 100)
        self.bar.setStyleSheet(f"QProgressBar{{background:{C.surface_sunken};border:none;border-radius:5px;}}"
                               f"QProgressBar::chunk{{background:{C.accent};border-radius:5px;}}")
        root.addWidget(self.bar)
        self.detail = label("", "meta", wrap=True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.detail)
        self.message = label("", wrap=True)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.message)

        if releases:
            root.addWidget(label(tr("What is in this update"), "subsection"))
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setMinimumHeight(140)
            scroll.setMaximumHeight(260)
            body = QWidget()
            column = QVBoxLayout(body)
            column.setSpacing(S.xs)
            for release in releases:
                column.addWidget(label(f"v{release.get('version', '')}  {release.get('date', '')}".strip(), "subsection"))
                for note in updates.notes_for(release)[:30]:
                    line = label("• " + note, wrap=True)
                    line.setTextFormat(Qt.TextFormat.PlainText)
                    line.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                    column.addWidget(line)
            column.addStretch(1)
            scroll.setWidget(body)
            root.addWidget(scroll)

        buttons = QHBoxLayout()
        self.copy_button = make_button(tr("Copy update notes"), "ghost", "copy", callback=self.copy_notes)
        self.copy_button.setVisible(bool(releases))
        self.folder_button = make_button(tr("Open downloads folder"), "ghost", callback=self._open_folder)
        self.folder_button.hide()
        self.close_button = make_button(tr("Close"), "secondary", callback=self.accept)
        self.close_button.setEnabled(False)
        buttons.addWidget(self.copy_button)
        buttons.addWidget(self.folder_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        root.addLayout(buttons)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)

    # -- running -----------------------------------------------------------------------------
    def start(self) -> None:
        """Start the helper (hidden window) and begin watching its progress file."""
        try:
            (self._data / updates.PROGRESS_NAME).unlink(missing_ok=True)       # never show an earlier run
        except OSError:
            pass
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._process = subprocess.Popen(self._args, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as error:
            self._fail(tr("The update helper could not be started: {error}", error=type(error).__name__))
            return
        self.timer.start(POLL_MS)
        self.poll()

    def done(self, result: int) -> None:      # noqa: D102 — closing is only possible once the helper is no longer working
        self.timer.stop()
        super().done(result)

    def reject(self) -> None:
        if self.close_button.isEnabled():
            super().reject()

    def _progress(self) -> dict[str, Any]:
        try:
            data = json.loads(runtime.read_private(self._data / updates.PROGRESS_NAME, limit=65536))
        except (OSError, ValueError):
            return self._last
        return data if isinstance(data, dict) else self._last

    def poll(self) -> None:
        data = self._progress()
        self._last = data
        code = self._process.poll() if self._process is not None else None
        state = str(data.get("state", "running"))
        if code is not None and code != 0:
            if state != "failed":
                data = {**data, "state": "failed", "message": tr("The update helper stopped unexpectedly (exit code {code}).", code=code)}
            state = "failed"
        elif self._process is not None and code is None and state in ("current", "launched", "done"):
            state = "running"           # the progress file cannot certify success before the helper exits
        elif code == 0 and state == "running":
            state = "done"
        self._state = state
        self.render(data)

    def in_progress(self) -> bool:
        return self._state in ("running", "launched")

    def _fail(self, text: str) -> None:
        self.timer.stop()
        self._state = "failed"
        self.render({"state": "failed", "message": text, "stages": [], "step": 0})

    # -- display -----------------------------------------------------------------------------
    def render(self, data: dict[str, Any]) -> None:
        stages = [str(s) for s in data.get("stages", [])] if isinstance(data.get("stages"), list) else []
        step = int(data.get("step", 0) or 0)
        state = self._state
        latest = str(data.get("to") or (self._releases[0].get("version") if self._releases else ""))
        self.versions.setText(f"v{data.get('from', '')} → v{latest}" if latest else f"v{data.get('from', '')}")
        while len(self.step_labels) < len(stages):
            row = label("", wrap=True)
            self.step_labels.append(row)
            self.steps.addWidget(row)
        for index, text in enumerate(stages, start=1):
            if state == "failed" and index == step:
                kind = "fail"
            elif index < step or (index == step and state in ("done", "current")):
                kind = "ok"
            elif index == step:
                kind = "run"
            else:
                kind = "todo"
            row = self.step_labels[index - 1]
            row.setText(f"{MARK[kind]}  {index}. {text}")
            set_tone(row, TONE[kind])
        self._bar(data, stages, step, state)
        self._texts(data, state)
        stalled = self._launched_at is not None and time.monotonic() - self._launched_at >= STALL_SECONDS
        finished = state in ("failed", "current", "done")
        self.close_button.setEnabled(finished or stalled)         # never trap the user if the installer never closes the launcher
        if state == "launched" and self._launched_at is None:
            self._launched_at = time.monotonic()
            self.launched.emit()
        if finished:
            self.timer.stop()

    def _bar(self, data: dict[str, Any], stages: list[str], step: int, state: str) -> None:
        done, total = int(data.get("done", 0) or 0), int(data.get("total", 0) or 0)
        if state == "launched":
            self.bar.setRange(0, 0)                   # the installer's own progress is out of our hands: keep it moving
        elif state in ("done", "current"):
            self.bar.setRange(0, 100)
            self.bar.setValue(100)
        elif stages and step == 3 and total and state == "running":
            self.bar.setRange(0, 100)
            self.bar.setValue(min(100, done * 100 // total))
        elif state == "failed":
            self.bar.setRange(0, 100)
            self.bar.setValue(0)
        else:
            self.bar.setRange(0, 0)

    def _texts(self, data: dict[str, Any], state: str) -> None:
        done, total = int(data.get("done", 0) or 0), int(data.get("total", 0) or 0)
        step = int(data.get("step", 0) or 0)
        detail = str(data.get("detail", ""))
        if state == "running" and step == 3 and total:
            text = tr("{done} of {total} downloaded", done=megabytes(done), total=megabytes(total))
            if detail and "checksum" not in detail:
                text = f"{detail}\n{text}"
            detail = text
        self.detail.setText(detail)
        message, tone = str(data.get("message", "")), ""
        if state == "launched":
            elapsed = int(time.monotonic() - (self._launched_at or time.monotonic()))
            message = tr("The installer is closing Vibe Launcher, replacing its files and will reopen it by itself. "
                         "This usually takes under a minute. Please do not shut down the computer. ({seconds} s)", seconds=elapsed)
            tone = "accent"
            if elapsed >= STALL_SECONDS:
                message += "\n" + tr("Taking longer than usual. If nothing happens, open the downloads folder and run the installer by hand; your data is backed up.")
                self.folder_button.show()
        elif state == "failed":
            tone = "danger"
            self.folder_button.setVisible(step >= 4)
        elif state in ("current", "done"):
            tone = "ok"
        self.message.setText(message)
        set_tone(self.message, tone)

    # -- actions -----------------------------------------------------------------------------
    def copy_notes(self) -> None:
        QGuiApplication.clipboard().setText(updates.release_text(self._releases))
        self.copy_button.setText(tr("Copied"))
        later(self, 1500, lambda: self.copy_button.setText(tr("Copy update notes")))

    def _open_folder(self) -> None:
        folder = self._data / updates.UPDATES_DIR
        if folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.fspath(folder)))
