"""Built-in terminal: xterm.js in a QWebEngineView, driven by a ConPTY.

Security stance: everything a remote host prints is untrusted data. It is only ever fed to xterm.js as bytes (never as
HTML), the page may load nothing but the packaged local files, and navigation is locked. The page talks to Python
through a tiny QWebChannel bridge (keystrokes in, size/title/copy/chord out).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import weakref
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import QApplication, QMenu, QVBoxLayout, QWidget

from . import conpty, theme
from .theme import C

log = logging.getLogger(__name__)
ASSETS = Path(__file__).with_name("assets")
_ILLEGAL_TITLE = re.compile(r"[\x00-\x1f\x7f]")
FLUSH_IDLE_MS = 4
FLUSH_MAX_MS = 16
PRESENT_DELAY_MS = 34
OUTPUT_LIMIT = 262144
OUTPUT_BATCH = 131072


class _OutputBuffer:
    """Reader backpressure includes bytes waiting for Qt, Chromium and xterm's write callback."""

    def __init__(self, limit: int = OUTPUT_LIMIT):
        self.limit = limit
        self._condition = threading.Condition()
        self._pending = bytearray()
        self._inflight = 0
        self._closed = False
        self.peak = 0

    @property
    def outstanding(self) -> int:
        with self._condition:
            return len(self._pending) + self._inflight

    def write(self, data: bytes, notify) -> None:
        offset = 0
        while offset < len(data):
            with self._condition:
                self._condition.wait_for(lambda: self._closed or
                                         len(self._pending) + self._inflight < self.limit)
                if self._closed:
                    return
                outstanding = len(self._pending) + self._inflight
                count = min(len(data) - offset, self.limit - outstanding)
                self._pending.extend(data[offset:offset + count])
                offset += count
                self.peak = max(self.peak, outstanding + count)
            # Only the empty -> nonempty transition posts a Qt event, never one event per tiny write.
            if outstanding == 0:
                notify()

    def take(self) -> bytes:
        with self._condition:
            if self._inflight:
                return b""
            chunk = bytes(self._pending[:OUTPUT_BATCH])
            del self._pending[:len(chunk)]
            self._inflight = len(chunk)
            return chunk

    def acknowledge(self) -> None:
        with self._condition:
            self._inflight = 0
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

_webengine_ok: bool | None = None


def available() -> bool:
    """Can this machine host a built-in terminal? (ConPTY + QtWebEngine)"""
    global _webengine_ok
    if _webengine_ok is None:
        try:
            import PySide6.QtWebChannel  # noqa: F401
            import PySide6.QtWebEngineWidgets  # noqa: F401
            _webengine_ok = True
        except ImportError:
            _webengine_ok = False
    return _webengine_ok and conpty.available()


def _windows_build() -> int:
    import sys
    return int(getattr(sys, "getwindowsversion", lambda: type("V", (), {"build": 0})())().build)


class TerminalPrefs:
    """Viewer preferences shared by every built-in terminal (set from the app's UI preferences)."""

    font_size = 14
    cursor_blink = False


FONT_SIZES = theme.TERMINAL_FONT_SIZES
_live: "weakref.WeakSet[TerminalWidget]" = weakref.WeakSet()


def configure(*, font_size: int | None = None, cursor_blink: bool | None = None) -> None:
    """Change the terminal preferences and apply them to every open terminal."""
    if font_size is not None and font_size in FONT_SIZES:
        TerminalPrefs.font_size = font_size
    if cursor_blink is not None:
        TerminalPrefs.cursor_blink = bool(cursor_blink)
    for widget in list(_live):
        widget.apply_preferences()


def retheme_all() -> None:
    """Repaint every open terminal in the active theme."""
    for widget in list(_live):
        widget.apply_theme()


def terminal_html() -> str:
    """The page, assembled from the packaged template with the active theme's colours and the viewer preferences."""
    from PySide6.QtCore import QFile, QIODevice
    channel = QFile(":/qtwebchannel/qwebchannel.js")
    script = ""
    if channel.open(QIODevice.OpenModeFlag.ReadOnly):
        script = bytes(channel.readAll()).decode("utf-8")
        channel.close()
    chrome = theme.terminal_chrome()
    html = (ASSETS / "terminal.html").read_text(encoding="utf-8")
    for key, value in (("__BACKGROUND__", chrome["bg"]), ("__SURFACE__", chrome["surface"]), ("__TEXT__", chrome["text"]),
                       ("__ACCENT__", chrome["accent"]), ("__THUMB__", chrome["thumb"]),
                       ("__THEME__", json.dumps(theme.terminal_theme())), ("__FONT_SIZE__", str(TerminalPrefs.font_size)),
                       ("__CURSOR_BLINK__", "true" if TerminalPrefs.cursor_blink else "false"),
                       ("__WINDOWS_BUILD__", str(_windows_build())),
                       ("__WEBGL__", "true" if os.environ.get("VIBE_TERMINAL_WEBGL") == "1" else "false")):
        html = html.replace(key, value)
    return html.replace("__QWEBCHANNEL__", script.replace("</script", "<\\/script"))


class _Bridge(QObject):
    """What the page may call. Keep it small: it is the only way untrusted terminal content reaches Python."""

    input_text = Signal(str)
    input_binary = Signal(str)
    size_changed = Signal(int, int)
    titled = Signal(str)
    belled = Signal()
    copied = Signal(str)
    chorded = Signal(str)
    ready_sig = Signal(int, int)
    written_sig = Signal(int)

    @Slot(int)
    def written(self, sequence: int) -> None:
        self.written_sig.emit(sequence)

    @Slot(str)
    def input(self, data: str) -> None:
        self.input_text.emit(data)

    @Slot(str)
    def binary(self, data: str) -> None:
        self.input_binary.emit(data)

    @Slot(int, int)
    def resized(self, cols: int, rows: int) -> None:
        self.size_changed.emit(cols, rows)

    @Slot(int, int)
    def ready(self, cols: int, rows: int) -> None:
        self.ready_sig.emit(cols, rows)

    @Slot(str)
    def title(self, text: str) -> None:
        self.titled.emit(text)

    @Slot()
    def bell(self) -> None:
        self.belled.emit()

    @Slot(str)
    def copy(self, text: str) -> None:
        self.copied.emit(text)

    @Slot(str)
    def chord(self, name: str) -> None:
        self.chorded.emit(name)


class _GuardShortcuts(QObject):
    """Keys typed into a terminal belong to the program in it, never to an application shortcut."""

    def eventFilter(self, watched, event):  # noqa: N802
        kind = event.type()
        if kind in (QEvent.Type.ShortcutOverride, QEvent.Type.FocusIn) and isinstance(watched, QWidget):
            widget = watched
            while widget is not None:
                if isinstance(widget, TerminalWidget):
                    if kind == QEvent.Type.FocusIn:
                        widget.focused.emit()   # lets the workspace follow the terminal the user clicked
                        return False
                    event.accept()
                    return True
                widget = widget.parentWidget()
        return False


_guard: _GuardShortcuts | None = None
_profile = None


def _shared_profile():
    """One off-the-record profile (nothing is written to disk) shared by every terminal."""
    global _profile
    if _profile is None:
        from PySide6.QtWebEngineCore import QWebEngineProfile
        _profile = QWebEngineProfile(QApplication.instance())  # unnamed => off the record
        _profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        _profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
    return _profile


_warm_page = None


def warm_up() -> None:
    """Start the browser engine in the background so the first terminal does not wait ~5 seconds for Chromium."""
    global _warm_page
    if _warm_page is not None or not available():
        return
    from PySide6.QtWebEngineCore import QWebEnginePage
    _warm_page = QWebEnginePage(_shared_profile(), QApplication.instance())
    _warm_page.setHtml("<!doctype html><title>warm</title>")


def release_warm_page() -> None:
    """Free the warm-up page before the profile goes away (Qt warns if a page outlives its profile)."""
    global _warm_page
    if _warm_page is not None:
        _warm_page.deleteLater()
        _warm_page = None


class TerminalWidget(QWidget):
    """One terminal: spawn a command in a ConPTY and show it. Safe to create many; each owns its process tree."""

    exited = Signal(int)
    output_drained = Signal()
    title_changed = Signal(str)
    chord = Signal(str)
    activity = Signal()
    ready = Signal()
    focused = Signal()

    _output = Signal()
    _exit_code = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        global _guard
        from PySide6.QtWebChannel import QWebChannel
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView

        if _guard is None:
            _guard = _GuardShortcuts(QApplication.instance())
            QApplication.instance().installEventFilter(_guard)

        class LockedPage(QWebEnginePage):
            def acceptNavigationRequest(self, url, nav_type, is_main_frame):  # noqa: N802
                # The page is set once from our own HTML. Nothing the remote host prints may navigate it anywhere.
                blocked = (QWebEnginePage.NavigationType.NavigationTypeLinkClicked,
                           QWebEnginePage.NavigationType.NavigationTypeFormSubmitted)
                return nav_type not in blocked and url.scheme() in {"about", "data", "file", "qrc"}

            def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
                log.debug("terminal page: %s (%s:%s)", message, source, line)

        self._pty: conpty.ConPty | None = None
        self._launch: tuple[list[str], str | None, dict[str, str] | None] | None = None
        self._buffer = _OutputBuffer()
        self._write_sequence = 0
        self._awaiting_write = 0
        self._page_ready = False
        self._want_focus = False
        self._disposed = False
        # Output is handed to the page in batches: wait until the program pauses (FLUSH_IDLE_MS) but never longer than
        # FLUSH_MAX_MS, so a full-screen redraw usually arrives in one piece instead of as several half frames.
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.timeout.connect(self._flush)
        self._batch_age = QElapsedTimer()
        # Once xterm has drawn a batch (next animation frame), ask Qt to present the view again. Chromium's new frame
        # could otherwise sit unshown until the next key or mouse event.
        self._present_timer = QTimer(self)
        self._present_timer.setSingleShot(True)
        self._present_timer.setInterval(PRESENT_DELAY_MS)
        self._present_timer.timeout.connect(self._present)
        _live.add(self)
        self.exit_code: int | None = None
        self.cols, self.rows = 120, 30
        self.title = ""

        self._view = QWebEngineView(self)
        self._page = LockedPage(_shared_profile(), self._view)
        self._view.setPage(self._page)
        settings = self._page.settings()
        for attribute, value in ((QWebEngineSettings.WebAttribute.LocalStorageEnabled, False),
                                 (QWebEngineSettings.WebAttribute.PluginsEnabled, False),
                                 (QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, False),
                                 (QWebEngineSettings.WebAttribute.ErrorPageEnabled, False),
                                 (QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False),
                                 (QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False),
                                 (QWebEngineSettings.WebAttribute.WebGLEnabled, True),
                                 (QWebEngineSettings.WebAttribute.FocusOnNavigationEnabled, False)):
            settings.setAttribute(attribute, value)
        self._page.setBackgroundColor(C.bg)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._context_menu)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)

        self._bridge = _Bridge(self)
        self._channel = QWebChannel(self._page)
        self._channel.registerObject("bridge", self._bridge)
        self._page.setWebChannel(self._channel)
        self._bridge.input_text.connect(self._on_input)
        self._bridge.input_binary.connect(self._on_binary)
        self._bridge.size_changed.connect(self._on_resized)
        self._bridge.ready_sig.connect(self._on_ready)
        self._bridge.written_sig.connect(self._on_written)
        self._bridge.titled.connect(self._on_title)
        self._bridge.belled.connect(self.activity)
        self._bridge.copied.connect(lambda text: QApplication.clipboard().setText(text))
        self._bridge.chorded.connect(self.chord)
        self._output.connect(self._on_output)
        self._exit_code.connect(self._on_exit)
        self._page.setHtml(terminal_html(), QUrl.fromLocalFile(str(ASSETS / "xterm") + os.sep))

    # -- process ----------------------------------------------------------------------------
    def start(self, command: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        """Run ``command`` here. It really starts as soon as the page has measured itself (a few milliseconds)."""
        env = dict(os.environ if env is None else env)
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLORTERM", "truecolor")
        self._launch = (command, cwd, env)
        if self._page_ready:
            self._spawn()

    def _spawn(self) -> None:
        if self._launch is None or self._pty is not None:
            return
        command, cwd, env = self._launch
        self._launch = None
        try:
            self._pty = conpty.ConPty(command, cwd=cwd, env=env, cols=self.cols, rows=self.rows,
                                      on_output=self._queue_output, on_exit=self._exit_code.emit)
        except OSError as error:
            self._queue_output(f"\x1b[31m無法啟動終端：{error}\x1b[0m\r\n".encode("utf-8")[:8192])
            self.exit_code = -1
            self.exited.emit(-1)

    @property
    def pid(self) -> int:
        return self._pty.pid if self._pty else 0

    def is_running(self) -> bool:
        if self._launch is not None:  # waiting for the page to measure itself
            return self.exit_code is None
        return bool(self._pty and self.exit_code is None and self._pty.is_alive())

    def send_text(self, text: str) -> None:
        if self._pty and self.exit_code is None:
            self._pty.write(text.encode("utf-8"))

    def send_bytes(self, data: bytes) -> None:
        if self._pty and self.exit_code is None:
            self._pty.write(data)

    def detach_tmux(self) -> None:
        """Leave a tmux session running on the server: send its prefix (Ctrl+B) then ``d``."""
        self.send_bytes(b"\x02d")

    def terminate(self) -> None:
        self._buffer.close()  # wake a reader blocked on a stalled/closed page before stopping its process
        if self._launch is not None:  # never started: just report it as ended
            self._launch = None
            self.exit_code = -1
            self.exited.emit(-1)
        elif self._pty:
            self._pty.close()

    def note(self, text: str) -> None:
        """Print a dim status line in the terminal (not sent to the program)."""
        self._js("vl.note(%s)" % _js_string(text))

    def focus_terminal(self) -> None:
        self._want_focus = True
        if self._page_ready:
            self._view.setFocus(Qt.FocusReason.OtherFocusReason)
            self._js("vl.focus()")

    def clear_screen(self) -> None:
        self._js("vl.clear()")

    def paste_clipboard(self) -> None:
        text = QApplication.clipboard().text()
        if text:
            self._js("vl.paste(%s)" % _js_string(text))

    def copy_selection(self) -> None:
        self._page.runJavaScript("vl.selection()", lambda text: text and QApplication.clipboard().setText(text))

    # -- slots ------------------------------------------------------------------------------------
    def _queue_output(self, data: bytes) -> None:
        self._buffer.write(data, self._output.emit)

    @Slot()
    def _on_output(self) -> None:
        if self._disposed:
            return
        self.activity.emit()
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if not self._page_ready or self._disposed or self._awaiting_write or not self._buffer.outstanding:
            return
        if not self._flush_timer.isActive():
            self._batch_age.start()
            self._flush_timer.start(FLUSH_IDLE_MS)
        elif self._batch_age.elapsed() + FLUSH_IDLE_MS <= FLUSH_MAX_MS:
            self._flush_timer.start(FLUSH_IDLE_MS)       # still streaming: wait for the pause

    def _flush(self) -> None:
        if self._disposed or not self._page_ready or self._awaiting_write:
            return
        chunk = self._buffer.take()
        if not chunk:
            return
        self._write_sequence += 1
        self._awaiting_write = self._write_sequence
        self._page.runJavaScript("vl.write('%s', %d)" %
                                 (base64.b64encode(chunk).decode("ascii"), self._awaiting_write))

    @Slot(int)
    def _on_written(self, sequence: int) -> None:
        if self._disposed or not self._awaiting_write or sequence != self._awaiting_write:
            return
        self._awaiting_write = 0
        self._buffer.acknowledge()
        self._schedule_flush()
        self._present_timer.start()
        if self.exit_code is not None and not self._buffer.outstanding:
            self.output_drained.emit()

    def _present(self) -> None:
        if not self._disposed:
            (self._view.focusProxy() or self._view).update()

    @property
    def is_output_drained(self) -> bool:
        return self.exit_code is not None and not self._buffer.outstanding

    # -- appearance ---------------------------------------------------------------------------
    def apply_theme(self) -> None:
        self._page.setBackgroundColor(QColor(C.bg))
        self._js("vl.setTheme(%s, %s)" % (json.dumps(theme.terminal_theme()), json.dumps(theme.terminal_chrome())))

    def apply_preferences(self) -> None:
        self._js("vl.fontSize(%d); vl.cursorBlink(%s)" % (TerminalPrefs.font_size,
                                                          "true" if TerminalPrefs.cursor_blink else "false"))

    def submit(self, text: str, enter: bool = True) -> None:
        """Send text from the input bar as if typed (several lines as one paste), then Enter."""
        if self.exit_code is None:
            self._js("vl.submit(%s, %s)" % (_js_string(text), "true" if enter else "false"))

    @Slot(int)
    def _on_exit(self, code: int) -> None:
        self.exit_code = code
        self._schedule_flush()
        self.exited.emit(code)
        if not self._disposed and self.is_output_drained:
            self.output_drained.emit()

    @Slot(str)
    def _on_input(self, data: str) -> None:
        self.send_text(data)

    @Slot(str)
    def _on_binary(self, data: str) -> None:
        self.send_bytes(data.encode("latin-1", "replace"))

    @Slot(int, int)
    def _on_ready(self, cols: int, rows: int) -> None:
        self._page_ready = True
        self._on_resized(cols, rows)
        self._spawn()
        if self._want_focus:
            self.focus_terminal()
        self._schedule_flush()
        self.ready.emit()

    @Slot(int, int)
    def _on_resized(self, cols: int, rows: int) -> None:
        if cols < 2 or rows < 2:  # a hidden/collapsed page reports a degenerate size; keep the last good one
            return
        self.cols, self.rows = cols, rows
        if self._pty:
            self._pty.resize(cols, rows)

    @Slot(str)
    def _on_title(self, text: str) -> None:
        self.title = _ILLEGAL_TITLE.sub("", text)[:120]
        self.title_changed.emit(self.title)

    def _js(self, script: str) -> None:
        if not self._disposed and self._page_ready:   # `vl` only exists once the page has finished starting
            self._page.runJavaScript(script)

    def _context_menu(self, pos) -> None:
        menu = QMenu(self)
        copy = QAction("複製選取文字", menu)
        copy.triggered.connect(self.copy_selection)
        paste = QAction("貼上", menu)
        paste.triggered.connect(self.paste_clipboard)
        clear = QAction("清除畫面", menu)
        clear.triggered.connect(self.clear_screen)
        for action in (copy, paste, None, clear):
            menu.addSeparator() if action is None else menu.addAction(action)
        menu.exec(self._view.mapToGlobal(pos))

    # -- teardown ---------------------------------------------------------------------------------
    def dispose(self) -> None:
        """Stop the process and free the web page. The widget must not be used afterwards."""
        if self._disposed:
            return
        self._disposed = True
        _live.discard(self)
        self._flush_timer.stop()
        self._present_timer.stop()
        self.terminate()
        self._view.setPage(None)
        self._page.deleteLater()
        self._view.deleteLater()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.dispose()
        super().closeEvent(event)


def _js_string(text: str) -> str:
    import json
    return json.dumps(text)
