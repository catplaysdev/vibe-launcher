"""輸入列: native text entry under a terminal, handed to the program on Enter."""
import pytest
from PySide6.QtCore import Qt

from vibe_launcher.composer import ComposerBar
from vibe_launcher.workspace_page import WorkspacePage

from test_workspace import ACCOUNTS, ENVIRONMENTS, PROJECTS, FakeTerminal, open_tab


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
