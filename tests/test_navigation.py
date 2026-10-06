"""Keyboard focus navigation overlay: numbered live outlines, digit jumps, no screenshots."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QPlainTextEdit, QVBoxLayout, QWidget

from vibe_launcher import i18n
from vibe_launcher.navigation import FocusNavigation


class Pane(QWidget):
    def __init__(self):
        super().__init__()
        self.terminal = QFrame()
        self.terminal.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.terminal.setMinimumSize(200, 150)
        self.composer = SimpleNamespace(edit=QPlainTextEdit())
        layout = QVBoxLayout(self)
        layout.addWidget(self.terminal)
        layout.addWidget(self.composer.edit)

    def composer_visible(self):
        return self.composer.edit.isVisible()

    def set_composer_visible(self, visible):
        self.composer.edit.setVisible(visible)


class Workspace:
    def __init__(self, tabs):
        self._tabs = tabs
        self.selected = tabs[0].info.id
        self.area = SimpleNamespace(on_screen=lambda pane: True, mode="grid")

    def tabs(self):
        return list(self._tabs)

    def select(self, tab_id, focus=True):
        self.selected = tab_id

    def selected_id(self):
        return self.selected

    def tab(self, tab_id):
        return next((t for t in self._tabs if t.info.id == tab_id), None)


class FakeWindow(QWidget):
    def __init__(self):
        super().__init__()
        layout = QHBoxLayout(self)
        tabs = []
        for i, name in enumerate(("Codex · account", "Claude · other")):
            pane = Pane()
            layout.addWidget(pane)
            tabs.append(SimpleNamespace(info=SimpleNamespace(id=f"t{i}", label=name), pane=pane, terminal=pane.terminal))
        self.workspace = Workspace(tabs)
        self.resize(900, 600)

    def current_page(self):
        return "workspace"

    def centralWidget(self):  # noqa: N802
        return self


@pytest.fixture
def window(qtbot, monkeypatch):
    def no_grab(*args, **kwargs):
        raise AssertionError("navigation must not take screenshots")
    monkeypatch.setattr(QWidget, "grab", no_grab)
    i18n.set_language("en")
    w = FakeWindow()
    qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    return w


def test_overlay_numbers_every_candidate_and_names_them(window):
    tabs = window.workspace.tabs()
    tabs[0].terminal.setFocus()
    nav = FocusNavigation(window)
    nav.start()
    assert nav.active and nav.overlay.isVisible() and nav.target is tabs[0].terminal
    assert nav.overlay.geometry() == window.rect()
    entries = nav.entries()
    assert [e[0] for e in entries] == [1, 2, 3, 4]
    assert [e[3] for e in entries] == ["Codex · account", "Codex · account · Input bar",
                                       "Claude · other", "Claude · other · Input bar"]
    # Painting goes through QPainter only (QWidget.grab is patched to fail): the hint bar sits at the top centre.
    image = QImage(window.size(), QImage.Format.Format_ARGB32)
    image.fill(0)
    nav.overlay.render(image, QPoint())
    assert image.pixelColor(window.width() // 2, 24).alpha() > 0
    assert image.pixelColor(window.width() // 2, window.height() - 20).alpha() == 0
    nav.stop()
    assert not nav.overlay.isVisible()


def test_digit_jumps_and_escape_restores_original_focus(window, qtbot):
    tabs = window.workspace.tabs()
    original = tabs[0].pane.composer.edit
    original.setFocus()
    nav = FocusNavigation(window)
    nav.start()
    assert nav.target is original
    qtbot.keyClick(original, Qt.Key.Key_3)
    assert nav.target is tabs[1].terminal and window.workspace.selected_id() == "t1"
    assert original.toPlainText() == ""
    qtbot.keyClick(original, Qt.Key.Key_9)  # no ninth target: nothing changes
    assert nav.target is tabs[1].terminal
    qtbot.keyClick(original, Qt.Key.Key_Escape)
    assert not nav.active and not nav.overlay.isVisible()
    assert QApplication.focusWidget() is original and window.workspace.selected_id() == "t0"


def test_enter_confirms_and_overlay_follows_resize(window, qtbot):
    tabs = window.workspace.tabs()
    tabs[0].terminal.setFocus()
    nav = FocusNavigation(window)
    nav.start()
    qtbot.keyClick(tabs[0].terminal, Qt.Key.Key_2)
    assert nav.target is tabs[0].pane.composer.edit
    window.resize(1000, 700)
    assert nav.overlay.geometry() == window.rect()
    qtbot.keyClick(tabs[0].terminal, Qt.Key.Key_Return)
    assert not nav.active and not nav.overlay.isVisible()
    assert QApplication.focusWidget() is tabs[0].pane.composer.edit
