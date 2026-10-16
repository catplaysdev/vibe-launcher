"""The built-in terminal: engine, flow control, ConPTY, composer, drafts and zoom.

- terminal engine
- terminal flow: Bounded, lossless transfer, including a deliberately stalled browser consumer.
- conpty: The ConPTY layer against real processes (Windows only).
- composer: 輸入列: native text entry under a terminal, handed to the program on Enter.
- drafts
- zoom: Terminal text zoom (shortcuts / Ctrl+wheel), the whole-window interface scale, and the cheaper status poll.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from test_workspace import ACCOUNTS, ENVIRONMENTS, PROJECTS, FakeTerminal, open_tab, SpawnService
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QWidget
from vibe_launcher import conpty, runtime, shortcuts, terminal as terminal_module
from vibe_launcher.composer import ComposerBar
from vibe_launcher.drafts import clear_draft, draft_path, FormDraft
from vibe_launcher.prefs import stored_ui_scale, UiPrefs
from vibe_launcher.settings_dialog import SettingsDialog
from vibe_launcher.store import Store
from vibe_launcher.terminal import _OutputBuffer, TerminalWidget
from vibe_launcher.theme import TERMINAL_FONT_SIZES
from vibe_launcher.ui import MainWindow
from vibe_launcher.workspace_page import WorkspacePage


# --- terminal engine --------------------------------------------------------------------------------------------------
SRC = Path(__file__).resolve().parents[1] / "src"


def test_checking_for_the_built_in_terminal_does_not_load_the_browser_engine():
    # Qt WebEngine maps about 200 MB of code; an idle launcher must not carry it until a terminal opens.
    probe = ("import sys; sys.path.insert(0, %r); from vibe_launcher import terminal; "
             "print(terminal.available() in (True, False), 'PySide6.QtWebEngineCore' in sys.modules)" % str(SRC))
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    assert result.stdout.split() == ["True", "False"], result.stderr


# --- terminal flow ----------------------------------------------------------------------------------------------------
def test_reader_waits_for_browser_ack_and_preserves_split_utf8_and_controls():
    buffer = _OutputBuffer(limit=257)
    payload = ("中文\x1b[31m測試\x1b[0m\r\n" * 1000).encode()
    notified = threading.Event()
    finished = threading.Event()

    def produce():
        for offset in range(0, len(payload), 13):
            buffer.write(payload[offset:offset + 13], notified.set)
        finished.set()

    producer = threading.Thread(target=produce, daemon=True)
    producer.start()
    received = bytearray()
    try:
        assert notified.wait(2)
        deadline = time.monotonic() + 2
        while buffer.outstanding < buffer.limit and time.monotonic() < deadline:
            time.sleep(0.001)
        assert buffer.outstanding == buffer.limit
        chunk = buffer.take()
        assert chunk and not buffer.take()
        assert not finished.wait(0.05), "take must not free the browser's in-flight budget"
        received.extend(chunk)
        buffer.acknowledge()
        deadline = time.monotonic() + 5
        while not finished.is_set() or buffer.outstanding:
            assert time.monotonic() < deadline
            chunk = buffer.take()
            if chunk:
                received.extend(chunk)
                buffer.acknowledge()
            else:
                time.sleep(0.001)
        assert received == payload
        assert buffer.peak == buffer.limit
    finally:
        buffer.close()
        producer.join(2)
    assert not producer.is_alive()


def test_dispose_unblocks_reader_even_when_browser_never_acknowledges():
    buffer = _OutputBuffer(limit=8)
    notified = threading.Event()
    producer = threading.Thread(target=lambda: buffer.write(b"x" * 32, notified.set), daemon=True)
    producer.start()
    assert notified.wait(2)
    assert buffer.take() == b"x" * 8
    buffer.close()
    producer.join(2)
    assert not producer.is_alive()
    assert buffer.outstanding == 8  # closing does not silently discard already accepted bytes


def test_small_writes_do_not_flood_qt_notifications():
    buffer = _OutputBuffer(limit=1000)
    signals = []
    for _ in range(1000):
        buffer.write(b"a", lambda: signals.append(True))
    assert len(signals) == 1
    assert buffer.take() == b"a" * 1000
    buffer.acknowledge()
    buffer.write(b"b", lambda: signals.append(True))
    assert len(signals) == 2


def test_stale_or_duplicate_ack_does_not_release_next_batch():
    buffer = _OutputBuffer(limit=8)
    buffer.write(b"12345678", lambda: None)
    buffer.take()
    scheduled, presented = [], []
    widget = SimpleNamespace(_disposed=False, _awaiting_write=2, _buffer=buffer,
                             _display_timer=SimpleNamespace(stop=lambda: None),
                             exit_code=None, _present_timer=SimpleNamespace(start=lambda: presented.append(True)),
                             _schedule_flush=lambda: scheduled.append(True))
    TerminalWidget._on_written(widget, 1)
    assert buffer.outstanding == 8
    TerminalWidget._on_written(widget, 2)
    assert buffer.outstanding == 0 and scheduled == [True] and presented == [True]
    TerminalWidget._on_written(widget, 2)
    assert scheduled == [True] and presented == [True]


def test_exit_output_is_drained_only_after_browser_ack():
    buffer = _OutputBuffer(limit=8)
    buffer.write(b"last", lambda: None)
    buffer.take()
    drained = []
    widget = SimpleNamespace(_disposed=False, _awaiting_write=1, _buffer=buffer, exit_code=0,
                             _display_timer=SimpleNamespace(stop=lambda: None),
                             _schedule_flush=lambda: None, _present_timer=SimpleNamespace(start=lambda: None),
                             output_drained=SimpleNamespace(emit=lambda: drained.append(True)))
    assert not TerminalWidget.is_output_drained.fget(widget)
    TerminalWidget._on_written(widget, 1)
    assert TerminalWidget.is_output_drained.fget(widget) and drained == [True]
    TerminalWidget._on_written(widget, 1)
    assert drained == [True]


# --- conpty -----------------------------------------------------------------------------------------------------------
class Run:
    """Start a command in a ConPTY and collect its raw output and exit code."""

    def __init__(self, command, env=None, cwd=None):
        self.chunks, self.exits, self.pty = [], [], None
        self.pty = conpty.ConPty(command, cwd=cwd, env=env or dict(os.environ, PYTHONUTF8="1"), cols=100, rows=24,
                                 on_output=self._output, on_exit=self.exits.append)

    def _output(self, data):
        self.chunks.append(data)
        if b"\x1b[c" in data and self.pty:        # terminals answer this query; conhost waits for it
            self.pty.write(b"\x1b[?1;2c")

    def wait(self, seconds=15):
        end = time.time() + seconds
        while not self.exits and time.time() < end:
            time.sleep(0.05)
        return self

    def until(self, needle: bytes, seconds=10):
        end = time.time() + seconds
        while needle not in self.output and time.time() < end:
            time.sleep(0.05)
        return needle in self.output

    @property
    def output(self) -> bytes:
        return b"".join(self.chunks)


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


@pytest.mark.skipif(not conpty.available(), reason="needs Windows ConPTY")
def test_output_reaches_us_as_raw_bytes_and_the_exit_code_propagates():
    run = Run(py("import sys; sys.stdout.buffer.write('你好，世界 ok'.encode('utf-8')); sys.stdout.flush(); sys.exit(7)")).wait()
    assert run.exits == [7]
    assert "你好，世界 ok".encode("utf-8") in run.output, "UTF-8 must pass through untouched (we never decode it here)"


@pytest.mark.skipif(not conpty.available(), reason="needs Windows ConPTY")
def test_the_child_really_has_a_terminal():
    run = Run(py("import os,sys; sys.stdout.write('T=%s,%s' % (os.isatty(0), os.isatty(1)))")).wait()
    assert b"T=True,True" in run.output


@pytest.mark.skipif(not conpty.available(), reason="needs Windows ConPTY")
def test_the_child_does_not_inherit_our_redirected_stdout(capfd):
    Run(py("print('only-on-the-pty')")).wait()
    assert "only-on-the-pty" not in capfd.readouterr().out


@pytest.mark.skipif(not conpty.available(), reason="needs Windows ConPTY")
def test_typing_reaches_an_interactive_program():
    run = Run(py("name = input('name> '); print('hello', name)"))
    assert run.until(b"name>")  # the trailing space may be sent as a cursor move
    run.pty.write("世界\r".encode("utf-8"))
    run.wait()
    assert "hello 世界".encode("utf-8") in run.output and run.exits == [0]


@pytest.mark.skipif(not conpty.available(), reason="needs Windows ConPTY")
def test_resizing_reaches_the_child():
    code = ("import os,time,sys\n"
            "print('A', os.get_terminal_size().columns, flush=True)\n"
            "time.sleep(1.2)\n"
            "print('B', os.get_terminal_size().columns, flush=True)\n")
    run = Run(py(code))
    assert run.until(b"A 100")
    run.pty.resize(137, 40)
    run.wait()
    assert b"B 137" in run.output


@pytest.mark.skipif(not conpty.available(), reason="needs Windows ConPTY")
def test_exit_is_reported_once_after_all_output():
    run = Run(py("import sys; print('last line'); sys.exit(3)")).wait()
    time.sleep(0.5)
    assert run.exits == [3] and b"last line" in run.output


@pytest.mark.skipif(not conpty.available(), reason="needs Windows ConPTY")
def test_terminate_kills_the_whole_process_tree():
    parent = ("import subprocess,sys,time\n"
              "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
              "print('child', child.pid, flush=True)\n"
              "time.sleep(60)\n")
    run = Run(py(parent))
    assert run.until(b"child ")
    text = run.output.decode("utf-8", "replace")
    child_pid = int(text.split("child ")[1].split()[0])
    run.pty.terminate()
    run.wait(10)
    assert run.exits, "the exit callback must fire after terminate()"
    time.sleep(0.5)
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x1000, False, child_pid)
    alive = False
    if handle:
        code = wintypes.DWORD()
        alive = bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        kernel.CloseHandle(handle)
    assert not alive, "the grandchild (think ssh.exe) must die with its terminal"


@pytest.mark.skipif(not conpty.available(), reason="needs Windows ConPTY")
def test_unknown_program_raises_instead_of_hanging():
    with pytest.raises(OSError):
        conpty.ConPty(["definitely-not-a-real-program-xyz.exe"], env=dict(os.environ), cols=80, rows=24)


@pytest.mark.skipif(not conpty.available(), reason="needs Windows ConPTY")
def test_writing_after_exit_is_harmless():
    run = Run(py("pass")).wait()
    run.pty.write(b"ignored")
    run.pty.resize(80, 24)


# --- composer ---------------------------------------------------------------------------------------------------------
class Recorder:
    def __init__(self, bar):
        self.submitted, self.raw, self.chords = [], [], []
        bar.submitted.connect(self.submitted.append)
        bar.raw.connect(self.raw.append)
        bar.chord.connect(self.chords.append)


@pytest.fixture
def bar(qtbot):
    widget = ComposerBar()
    qtbot.addWidget(widget)
    widget.show()
    widget.edit.setFocus()
    return widget


def test_enter_sends_and_clears_shift_enter_adds_a_line(bar, qtbot):
    seen = Recorder(bar)
    bar.edit.insertPlainText("第一行")  # QTest cannot type CJK; an input method commits text like this
    qtbot.keyClick(bar.edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    qtbot.keyClicks(bar.edit, "second")
    qtbot.keyClick(bar.edit, Qt.Key.Key_Return)
    assert seen.submitted == ["第一行\nsecond"] and bar.edit.toPlainText() == ""


def test_an_empty_box_passes_steering_keys_to_the_program(bar, qtbot):
    seen = Recorder(bar)
    qtbot.keyClick(bar.edit, Qt.Key.Key_Return)
    qtbot.keyClick(bar.edit, Qt.Key.Key_Up)
    qtbot.keyClick(bar.edit, Qt.Key.Key_Down)
    qtbot.keyClick(bar.edit, Qt.Key.Key_Tab)
    qtbot.keyClick(bar.edit, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
    assert seen.raw == [b"\r", b"\x1b[A", b"\x1b[B", b"\t", b"\x1b[Z"] and not seen.submitted


def test_escape_and_ctrl_c_always_reach_the_program_but_the_draft_stays(bar, qtbot):
    seen = Recorder(bar)
    bar.edit.insertPlainText("草稿")
    qtbot.keyClick(bar.edit, Qt.Key.Key_Escape)
    qtbot.keyClick(bar.edit, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClick(bar.edit, Qt.Key.Key_Up)                     # with text, arrows move the caret instead
    assert seen.raw == [b"\x1b", b"\x03"] and bar.edit.toPlainText() == "草稿"
    bar.edit.selectAll()
    qtbot.keyClick(bar.edit, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert seen.raw == [b"\x1b", b"\x03"], "with a selection Ctrl+C copies"


def test_ctrl_up_recalls_what_was_sent(bar, qtbot):
    for text in ("one", "two"):
        qtbot.keyClicks(bar.edit, text)
        qtbot.keyClick(bar.edit, Qt.Key.Key_Return)
    qtbot.keyClicks(bar.edit, "draft")
    qtbot.keyClick(bar.edit, Qt.Key.Key_Up, Qt.KeyboardModifier.ControlModifier)
    assert bar.edit.toPlainText() == "two"
    qtbot.keyClick(bar.edit, Qt.Key.Key_Up, Qt.KeyboardModifier.ControlModifier)
    assert bar.edit.toPlainText() == "one"
    qtbot.keyClick(bar.edit, Qt.Key.Key_Down, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClick(bar.edit, Qt.Key.Key_Down, Qt.KeyboardModifier.ControlModifier)
    assert bar.edit.toPlainText() == "draft"


def test_workspace_chords_work_from_the_box(bar, qtbot):
    seen = Recorder(bar)
    qtbot.keyClick(bar.edit, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClick(bar.edit, Qt.Key.Key_3, Qt.KeyboardModifier.AltModifier)
    qtbot.keyClick(bar.edit, Qt.Key.Key_E, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert seen.chords == ["next", "goto:3", "composer"]


def test_the_box_grows_with_its_text_up_to_a_limit(bar, qtbot):
    one = bar.edit.height()
    bar.edit.setPlainText("a\nb\nc")
    three = bar.edit.height()
    bar.edit.setPlainText("\n".join("x" * 20))
    assert one < three < bar.edit.height() and bar.edit.height() < one * 7


# ----------------------------------------------------------------------------- in a terminal pane


class SubmittingTerminal(FakeTerminal):
    def __init__(self):
        super().__init__()
        self.submitted = []

    def submit(self, text):
        self.submitted.append(text)

    def send_bytes(self, data):
        self.sent.append(data)


@pytest.fixture
def page(qtbot):
    view = WorkspacePage(terminal_factory=SubmittingTerminal)
    qtbot.addWidget(view, before_close_func=lambda w: w.shutdown())
    view.resize(1200, 700)
    view.show()
    view.set_data(PROJECTS, ACCOUNTS, ENVIRONMENTS, [])
    view._confirm = lambda *a, **k: True
    return view


def test_agent_terminals_open_with_the_box_and_shells_without(page):
    agent = open_tab(page)
    shell = open_tab(page, mode="shell", account="", session="s2")
    assert agent.pane.composer_visible() and not shell.pane.composer_visible()
    page.composer_default = False
    assert not open_tab(page, session="s3").pane.composer_visible()


def test_the_box_feeds_its_terminal(page, qtbot):
    tab = open_tab(page)
    edit = tab.pane.composer.edit
    edit.insertPlainText("請幫我看這個錯誤")
    qtbot.keyClick(edit, Qt.Key.Key_Return)
    qtbot.keyClick(edit, Qt.Key.Key_Escape)
    assert tab.terminal.submitted == ["請幫我看這個錯誤"] and tab.terminal.sent == [b"\x1b"]


def test_the_shortcut_opens_the_box_and_an_ended_terminal_drops_it(page):
    tab = open_tab(page, mode="shell", account="")
    page._on_chord("composer")
    assert tab.pane.composer_visible()
    tab.terminal.finish(0)
    assert not tab.pane.composer_visible() and not tab.pane.composer_button.isEnabled()


# --- drafts -----------------------------------------------------------------------------------------------------------
def form(qtbot):
    host = QWidget()
    qtbot.addWidget(host)
    name, secret, choice, flag = QLineEdit(host), QLineEdit(host), QComboBox(host), QCheckBox(host)
    secret.setEchoMode(QLineEdit.EchoMode.Password)
    choice.addItem("A", "a")
    choice.addItem("B", "b")
    return host, {"name": name, "ssh_password": QLineEdit(host), "hidden": secret, "choice": choice, "flag": flag}


def test_draft_round_trip_skips_secrets(qtbot, tmp_path):
    host, fields = form(qtbot)
    draft = FormDraft(host, tmp_path, "k", fields)
    assert not draft.restored
    fields["name"].setText("工地")
    fields["ssh_password"].setText("pw")
    fields["hidden"].setText("pw2")
    fields["choice"].setCurrentIndex(1)
    fields["flag"].setChecked(True)
    draft.save()
    stored = draft_path(tmp_path, "k").read_text(encoding="utf-8")
    assert "pw" not in stored
    host2, again = form(qtbot)
    restored = FormDraft(host2, tmp_path, "k", again)
    assert restored.restored
    assert again["name"].text() == "工地" and again["choice"].currentData() == "b" and again["flag"].isChecked()
    assert again["ssh_password"].text() == "" and again["hidden"].text() == ""


def test_draft_saves_after_edits_and_clears(qtbot, tmp_path):
    host, fields = form(qtbot)
    draft = FormDraft(host, tmp_path, "k", fields)
    fields["name"].setText("x")
    qtbot.waitUntil(lambda: draft_path(tmp_path, "k").exists(), timeout=3000)
    fields["name"].setText("")
    draft.save()
    assert not draft_path(tmp_path, "k").exists()              # back to the start: nothing to keep
    fields["name"].setText("y")
    draft.save()
    clear_draft(tmp_path, "k")
    assert not draft_path(tmp_path, "k").exists()


def test_stale_or_broken_draft_is_ignored(qtbot, tmp_path):
    runtime.write_json(draft_path(tmp_path, "old"), {"saved": time.time() - 30 * 24 * 3600, "values": {"name": "old"}})
    host, fields = form(qtbot)
    assert not FormDraft(host, tmp_path, "old", fields).restored and fields["name"].text() == ""
    assert not draft_path(tmp_path, "old").exists()
    runtime.write_json(draft_path(tmp_path, "bad"), {"saved": "x", "values": []})
    assert not FormDraft(host, tmp_path, "bad", fields).restored


def test_record_dialog_keeps_input_until_the_record_is_saved(qtbot, tmp_path):
    from types import SimpleNamespace

    from vibe_launcher.ui import RecordDialog
    parent = QWidget()
    parent.store = SimpleNamespace(path=tmp_path / "store.sqlite3")
    qtbot.addWidget(parent)
    fields = [("name", "專案名稱", ""), ("path", "資料夾路徑", "")]
    dialog = RecordDialog("新增專案", fields, {}, None, parent, draft=True)
    dialog.fields["name"].setText("打到一半")
    dialog.reject()                                          # closed by accident (Esc) or a refused save
    again = RecordDialog("新增專案", fields, {}, None, parent, draft=True)
    assert again.fields["name"].text() == "打到一半" and again.draft.restored
    again._discard({})
    assert again.fields["name"].text() == ""
    third = RecordDialog("新增專案", fields, {}, None, parent, draft=True)
    assert not third.draft.restored
    plain = RecordDialog("新增專案", fields, {}, None, parent)
    assert plain.draft is None


# --- zoom -------------------------------------------------------------------------------------------------------------
@pytest.fixture
def view(tmp_path, qtbot):
    store = Store(tmp_path / "launcher.db")
    window = MainWindow(store, SpawnService(), terminal_factory=FakeTerminal)
    qtbot.addWidget(window, before_close_func=lambda w: w.workspace.shutdown())
    window.show()
    yield window
    terminal_module.configure(font_size=14)


def test_terminal_zoom_steps_clamps_resets_and_is_remembered(view):
    view.show_page("workspace")
    view.dispatch_shortcut("zoom.in")
    assert view.prefs.terminal_font_size == 15 == terminal_module.TerminalPrefs.font_size
    assert json.loads(view.prefs.path.read_text(encoding="utf-8"))["terminal_font_size"] == 15
    view.workspace._on_chord("zoom.out")          # from inside a terminal: routed back to the window
    view.workspace._on_chord("zoom.out")
    assert view.prefs.terminal_font_size == 13
    for _ in range(30):
        view.dispatch_shortcut("zoom.in")
    assert view.prefs.terminal_font_size == TERMINAL_FONT_SIZES.stop - 1
    view.dispatch_shortcut("zoom.reset")
    assert view.prefs.terminal_font_size == 14 == terminal_module.TerminalPrefs.font_size


def test_terminal_zoom_only_acts_on_the_workspace(view):
    view.show_page("projects")
    view.dispatch_shortcut("zoom.in")
    assert view.prefs.terminal_font_size == 14


def test_zoom_keys_reach_the_terminal_page():
    keys = {b["action"]: b for b in shortcuts.web_bindings()}
    assert (keys["zoom.in"]["key"], keys["zoom.out"]["key"], keys["zoom.reset"]["key"]) == ("=", "-", "0")
    assert all(keys[a]["ctrl"] and not keys[a]["shift"] for a in ("zoom.in", "zoom.out", "zoom.reset"))
    html = terminal_module.terminal_html()
    assert "'wheel'" in html and "zoom.in" in html


def test_interface_scale_saved_from_settings_and_read_before_start(view, monkeypatch):
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: False)     # "restart later"
    dialog = SettingsDialog(view)
    dialog.ui_scale.setCurrentIndex(dialog.ui_scale.findData(125))
    dialog.save()
    assert view.prefs.ui_scale == 125
    assert stored_ui_scale(view.prefs.path) == 125 and UiPrefs(view.prefs.path).ui_scale == 125


@pytest.mark.parametrize("stored", ["", '{"ui_scale": 133}', '{"ui_scale": true}', "[1]", "{"])
def test_unusable_interface_scale_falls_back_to_100(tmp_path, stored):
    path = tmp_path / "ui-state.json"
    path.write_text(stored, encoding="utf-8")
    assert stored_ui_scale(path) == 100 and stored_ui_scale(tmp_path / "missing.json") == 100


def test_list_many_matches_separate_lists(tmp_path):
    store = Store(tmp_path / "launcher.db")
    store.save("auth_states", {"id": "user:local", "status": "logged_in"})
    assert store.list_many("sessions", "auth_states") == {"sessions": store.list("sessions"),
                                                          "auth_states": store.list("auth_states")}


@pytest.mark.parametrize("key, modifiers", [
    ("Key_Equal", ("ControlModifier",)), ("Key_Plus", ("ControlModifier", "ShiftModifier")),
    ("Key_Plus", ("ControlModifier", "KeypadModifier")),
])
def test_ctrl_plus_counts_as_zoom_in(key, modifiers):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    mods = Qt.KeyboardModifier.NoModifier
    for name in modifiers:
        mods |= getattr(Qt.KeyboardModifier, name)
    event = QKeyEvent(QEvent.Type.KeyPress, getattr(Qt.Key, key), mods)
    assert shortcuts.action_for(event) == "zoom.in"
    minus = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Minus, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.KeypadModifier)
    assert shortcuts.action_for(minus) == "zoom.out"


def test_zoom_from_the_input_bar_and_the_window(view, qtbot):
    from PySide6.QtCore import Qt
    from vibe_launcher.workspace_page import TabInfo
    view.show_page("workspace")
    tab = view.workspace.add_terminal(TabInfo(id="t", title="Codex", kind="agent", tool="codex"), FakeTerminal())
    tab.pane.set_composer_visible(True, focus=True)
    qtbot.keyClick(tab.pane.composer.edit, Qt.Key.Key_Plus, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert view.prefs.terminal_font_size == 15
    qtbot.keyClick(tab.pane.composer.edit, Qt.Key.Key_Minus, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.KeypadModifier)
    assert view.prefs.terminal_font_size == 14
    view.workspace.rail.setFocus()
    qtbot.keyClick(view.workspace.rail, Qt.Key.Key_Plus, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert view.prefs.terminal_font_size == 15


def test_scale_preview_follows_the_choice_and_cancel_saves_nothing(view):
    dialog = SettingsDialog(view)
    dialog.ui_scale.setCurrentIndex(dialog.ui_scale.findData(100))
    same = dialog.scale_preview.pixmap().toImage()
    dialog.ui_scale.setCurrentIndex(dialog.ui_scale.findData(200))
    bigger = dialog.scale_preview.pixmap()
    assert not bigger.isNull() and bigger.toImage() != same
    assert bigger.width() / bigger.devicePixelRatio() <= SettingsDialog.PREVIEW.width()
    dialog.reject()
    assert view.prefs.ui_scale == 100
    assert "ui_scale" not in json.loads(view.prefs.path.read_text(encoding="utf-8")) if view.prefs.path.exists() else True


def test_saving_a_new_scale_offers_a_restart(view, monkeypatch):
    asked, restarted = [], []
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: asked.append(a) or True)
    monkeypatch.setattr(view, "restart_launcher", lambda: restarted.append(True) or True)
    dialog = SettingsDialog(view)
    dialog.save()                                    # scale unchanged: no question
    assert not asked
    dialog = SettingsDialog(view)
    dialog.ui_scale.setCurrentIndex(dialog.ui_scale.findData(150))
    dialog.save()
    assert len(asked) == 1 and restarted == [True] and stored_ui_scale(view.prefs.path) == 150


def test_restart_starts_the_launcher_without_the_old_scale(view, monkeypatch):
    from vibe_launcher import ui
    started = {}

    class FakeProcess:
        def setProgram(self, program): started["program"] = program
        def setArguments(self, args): started["args"] = args
        def setWorkingDirectory(self, path): pass
        def setProcessEnvironment(self, env): started["env"] = env
        def startDetached(self): return True, 1

    monkeypatch.setattr(ui, "QProcess", FakeProcess)
    monkeypatch.setattr(ui, "_scale_from_prefs", True)
    monkeypatch.setenv("QT_SCALE_FACTOR", "1.25")
    monkeypatch.setattr(view, "close", lambda: False)
    assert view.restart_launcher() is False and not started          # the user chose to stay
    monkeypatch.setattr(view, "close", lambda: True)
    assert view.restart_launcher() is True
    assert started["args"] == ["-m", "vibe_launcher.ui"] and not started["env"].contains("QT_SCALE_FACTOR")
    assert ui.applied_ui_scale() == 125


def test_gpu_primer_only_with_real_terminals(view, tmp_path, qtbot):
    from vibe_launcher import ui
    assert getattr(view, "_gpu_primer", None) is None                # fake terminals: nothing to prepare for
    if not terminal_module.available():
        pytest.skip("built-in terminal unavailable")
    store = Store(tmp_path / "real.db")
    window = MainWindow(store, SpawnService())
    qtbot.addWidget(window, before_close_func=lambda w: w.workspace.shutdown())
    primer = window._gpu_primer
    assert primer.size().width() == 1 and primer.focusPolicy() == ui.Qt.FocusPolicy.NoFocus
