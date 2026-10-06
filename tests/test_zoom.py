"""Terminal text zoom (shortcuts / Ctrl+wheel), the whole-window interface scale, and the cheaper status poll."""
import json

import pytest

from vibe_launcher import shortcuts, terminal as terminal_module
from vibe_launcher.prefs import UiPrefs, stored_ui_scale
from vibe_launcher.settings_dialog import SettingsDialog
from vibe_launcher.store import Store
from vibe_launcher.theme import TERMINAL_FONT_SIZES
from vibe_launcher.ui import MainWindow
from test_workspace import FakeTerminal, SpawnService


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
