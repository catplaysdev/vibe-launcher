"""Catalogs, configurable input navigation and terminal command profiles."""
import json
import re
from string import Formatter

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import QApplication

from vibe_launcher import i18n, runtime, settings, shortcuts, terminal
from vibe_launcher.composer import chord_for
from vibe_launcher.navigation import FocusNavigation, cycle_window
from vibe_launcher.prefs import UiPrefs
from vibe_launcher.service import LauncherService
from vibe_launcher.settings_dialog import SettingsDialog
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow
from vibe_launcher.workspace_page import TabInfo
from test_workspace import FakeTerminal, SpawnService


@pytest.fixture
def view(tmp_path, qtbot):
    store = Store(tmp_path / "launcher.db")
    store.save("accounts", {"id": "user", "name": "Example", "tool": "codex", "auth": "subscription"})
    store.save("presets", {"id": "project", "name": "Example project", "environment_id": "local", "path": str(tmp_path)})
    window = MainWindow(store, SpawnService(), terminal_factory=FakeTerminal)
    qtbot.addWidget(window, before_close_func=lambda w: w.workspace.shutdown())
    window.show()
    return window


@pytest.mark.parametrize("locale", i18n.LANGUAGES)
def test_catalogs_cover_sources_and_keep_format_fields(locale):
    english, translated = i18n.catalog("en"), i18n.catalog(locale)
    assert english.keys() == translated.keys()
    assert len(english) >= 930
    for key in english:
        assert isinstance(english[key], str) and isinstance(translated[key], str) and translated[key].strip()
        assert "\ufffd" not in translated[key] and not re.search(r"\?{2,}", translated[key]), key
        if re.search(r"\{[a-zA-Z_]\w*(?:[:!][^{}]*)?\}", english[key]):
            names = lambda text: {name for _, name, _, _ in Formatter().parse(text) if name is not None}
            assert names(english[key]) == names(translated[key]), key
        for pattern in (r"</?[a-zA-Z][^<>]*>", r"%(?:[-+0# ]*\d*(?:\.\d+)?)?[sd]"):
            assert re.findall(pattern, english[key]) == re.findall(pattern, translated[key]), key
    i18n.set_language("en")
    assert i18n.tr("Terminal") == "Terminal"
    i18n.set_language("zh-TW")
    assert i18n.tr("Terminal") == "終端機"


def test_english_ui_and_named_fields(view):
    i18n.set_language("en")
    dialog = SettingsDialog(view)
    assert dialog.windowTitle() == "Settings"
    assert dialog.nav.item(0).text() == "General"
    assert "Enter confirm" in i18n.tr("navigation.hint", up="W", down="S", left="A", right="D", input="Tab")
    dialog.close()


def test_language_and_controls_survive_restart(tmp_path):
    prefs = UiPrefs(tmp_path / "ui-state.json")
    prefs.language = "en"
    prefs.shortcuts = {"next": "Ctrl+Alt+L"}
    prefs.composer_position = "top"
    prefs.save()
    restored = UiPrefs(prefs.path)
    assert (restored.language, restored.shortcuts, restored.composer_position) == ("en", {"next": "Ctrl+Alt+L"}, "top")


@pytest.mark.parametrize("locale", i18n.LANGUAGES)
def test_registered_language_is_selectable_and_survives_restart(view, tmp_path, monkeypatch, locale):
    i18n.set_language(locale)
    dialog = SettingsDialog(view)
    assert dialog.windowTitle() == i18n.catalog(locale)["Settings"]
    assert dialog.language.findData(locale) >= 0
    dialog.reject()
    monkeypatch.delenv("VIBE_LAUNCHER_LANGUAGE", raising=False)
    monkeypatch.setenv("VIBE_LAUNCHER_HOME", str(tmp_path))
    prefs = UiPrefs(tmp_path / "ui-state.json")
    prefs.language = locale
    prefs.save()
    assert UiPrefs(prefs.path).language == locale
    assert i18n._initial_language() == locale
    assert i18n.tr("untranslated-example") == "untranslated-example"


@pytest.mark.parametrize("locale", ("de", "es"))
def test_long_translations_fit_narrow_settings(view, qtbot, locale):
    i18n.set_language(locale)
    dialog = SettingsDialog(view)
    qtbot.addWidget(dialog)
    dialog.resize(680, 520)
    dialog.show()
    for page_name in ("general", "keyboard"):
        dialog.show_page(page_name)
        qtbot.wait(30)
        page = dialog.stack.currentWidget()
        if page.widget().width() > page.viewport().width():
            assert page.horizontalScrollBar().isVisible() and page.horizontalScrollBar().maximum() > 0
        if page_name == "keyboard":
            for edit in dialog.key_edits.values():
                page.ensureWidgetVisible(edit)
                QApplication.processEvents()
                left = edit.mapTo(page.viewport(), QPoint()).x()
                assert left >= 0 and left + edit.width() <= page.viewport().width()
    dialog.reject()


def test_language_defaults_to_english_and_reads_saved_choice(tmp_path, monkeypatch):
    monkeypatch.delenv("VIBE_LAUNCHER_LANGUAGE", raising=False)
    monkeypatch.setenv("VIBE_LAUNCHER_HOME", str(tmp_path))
    assert i18n._initial_language() == "en"
    prefs = UiPrefs(tmp_path / "ui-state.json")
    prefs.language = "zh-TW"
    prefs.save()
    assert i18n._initial_language() == "zh-TW"


def test_remote_runtime_embeds_only_needed_locale_without_package_imports():
    import base64
    import lzma
    from pathlib import Path
    from vibe_launcher.service import _compressed_runtime
    source = Path(runtime.__file__).read_bytes()
    for locale in i18n.LANGUAGES:
        _, compressed = _compressed_runtime(source, locale=locale)
        shipped = lzma.decompress(base64.b85decode(compressed)).decode("utf-8")
        namespace = {"__name__": "preview_runtime"}
        exec(compile(shipped, "preview_runtime", "exec"), namespace)
        assert "from vibe_launcher" not in shipped
        assert namespace["_MESSAGES"] == {}
        key = "Git author email format is invalid"
        assert repr(i18n.catalog(locale)[key]) in shipped


@pytest.mark.parametrize("locale", i18n.LANGUAGES)
@pytest.mark.parametrize("action", ("launch", "github", "harness"))
def test_remote_helper_translations_fit_transport_limit(locale, action):
    from vibe_launcher.service import remote_command
    request = {"action": action, "language": locale, "path": "/home/user/example"}
    if action == "launch":
        request["account"] = {"id": "example", "tool": "codex", "auth": "subscription"}
    elif action == "github":
        request.update(github_account={"id": "example", "environment_id": "ssh"}, github_operation="login", environment_id="ssh")
    else:
        request.update(account={"id": "example", "tool": "codex", "auth": "subscription"}, harness_operation="status", environment_id="ssh")
    assert len(remote_command(request)) <= 28000


@pytest.mark.parametrize("locale", i18n.LANGUAGES)
def test_github_login_metadata_fits_translated_remote_helper(tmp_path, locale):
    i18n.set_language(locale)
    store = Store(tmp_path / "launcher.db")
    store.save("environments", {"id": "ssh", "name": "Example", "kind": "ssh", "ssh_target": "example.com"})
    store.save("github_accounts", {"id": "example", "name": "Example", "environment_id": "ssh"})
    launches = []
    LauncherService(store).start("github", github_account_id="example", environment_id="ssh",
                                 operation={"github_operation": "login"}, spawn=lambda args, context: launches.append(args))
    assert launches


def test_custom_shortcut_hints_update_with_controls(view):
    tab = view.workspace.add_terminal(TabInfo(id="one", title="One", kind="shell", tool="shell"), FakeTerminal())
    view.prefs.shortcuts = {"sidebar": "Ctrl+Alt+B", "composer": "Ctrl+Alt+E", "palette": "Ctrl+Alt+K"}
    view.apply_controls()
    assert view.sidebar.collapse_button._hint == "Ctrl+Alt+B"
    assert "Ctrl+Alt+E" in tab.pane.composer_button.toolTip()
    assert view.workspace.palette_button.text() == "Ctrl+Alt+K"


def test_custom_keys_match_qt_and_web_and_keep_tmux_prefix():
    from PySide6.QtCore import QEvent
    shortcuts.configure({"next": "Ctrl+Alt+L"})
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_L, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
    assert chord_for(event) == "next"
    binding = next(b for b in shortcuts.web_bindings() if b["action"] == "next")
    assert binding == {"action": "next", "key": "l", "ctrl": True, "alt": True, "shift": False, "meta": False}
    assert all(not (b["key"] in {"b", "c"} and b["ctrl"] and not b["alt"] and not b["shift"]) for b in shortcuts.web_bindings())
    assert '__SHORTCUTS__' not in terminal.terminal_html()
    assert '"action": "next", "key": "l"' in terminal.terminal_html()
    with pytest.raises(ValueError, match="conflict"):
        shortcuts.validate({"next": "Ctrl+K"})
    with pytest.raises(ValueError):
        shortcuts.validate({"next": "L"})


@pytest.mark.skipif(__import__("os").environ.get("VIBE_TEST_WEBENGINE") != "1", reason="opt-in: starts a real Chromium")
def test_custom_keys_reach_the_real_terminal_bridge(qtbot):
    widget = terminal.TerminalWidget()
    qtbot.addWidget(widget, before_close_func=lambda w: w.dispose())
    widget.resize(900, 500)
    widget.show()
    qtbot.waitUntil(lambda: widget._page_ready, timeout=20000)
    actions = []
    widget.chord.connect(actions.append)
    shortcuts.configure({"next": "Ctrl+Alt+L"})
    widget.apply_shortcuts()
    widget._page.runJavaScript("""
        document.querySelector('.xterm-helper-textarea').dispatchEvent(
            new KeyboardEvent('keydown', {key: 'l', code: 'KeyL', ctrlKey: true, altKey: true,
                                         bubbles: true, cancelable: true}));
    """)
    qtbot.waitUntil(lambda: actions == ["next"], timeout=10000)
    widget.dispose()


def test_settings_save_reset_and_reject_conflicts(view):
    dialog = SettingsDialog(view)
    dialog.key_edits["next"].setKeySequence(QKeySequence("Ctrl+K"))
    dialog.save()
    assert "conflict" in dialog.error.text()
    assert not (view.store.path.parent / "settings.json").exists()
    dialog.reset_shortcuts()
    dialog.position.setCurrentIndex(dialog.position.findData("top"))
    dialog.language.setCurrentIndex(dialog.language.findData("en"))
    dialog.save()
    assert view.prefs.shortcuts == {} and view.prefs.composer_position == "top"
    assert settings.load_settings(view.store.path.parent)["shells"]["linux"] == "bash"


def test_navigation_moves_between_inputs_without_sending_text(view, qtbot):
    view.show_page("workspace")
    tab = view.workspace.add_terminal(TabInfo(
        id="tab", title="Codex", kind="agent", tool="codex"), FakeTerminal())
    qtbot.wait(30)
    edit = tab.pane.composer.edit
    edit.setPlainText("unsent draft")
    edit.setFocus()
    nav = FocusNavigation(view)
    nav.start()
    assert nav.active and nav.target is edit and nav.overlay.isVisible()
    nav.move("up")
    assert nav.target is tab.terminal
    qtbot.keyClick(edit, Qt.Key.Key_Tab)
    assert nav.target is edit
    qtbot.keyClick(edit, Qt.Key.Key_D)
    assert edit.toPlainText() == "unsent draft" and not tab.terminal.sent
    qtbot.keyClick(edit, Qt.Key.Key_Return)
    assert not nav.active and QApplication.focusWidget() is edit
    tab.pane.set_composer_position("top")
    nav.start()
    nav.move("down")
    assert nav.target is tab.terminal
    nav.stop(restore=True)


def test_navigation_switches_tabs_and_highlights_new_terminal(view, qtbot):
    view.show_page("workspace")
    one = view.workspace.add_terminal(TabInfo(id="one", title="One", kind="shell", tool="shell"), FakeTerminal())
    two = view.workspace.add_terminal(TabInfo(id="two", title="Two", kind="shell", tool="shell"), FakeTerminal())
    view.workspace.select(one.info.id)
    qtbot.wait(30)
    nav = FocusNavigation(view)
    nav.start()
    nav.move("right")
    assert nav.target is two.terminal and view.workspace.selected_id() == "two"
    nav.stop()


def test_window_shortcut_activates_another_launcher(view, qtbot, monkeypatch):
    other = MainWindow(view.store, SpawnService(), terminal_factory=FakeTerminal)
    qtbot.addWidget(other, before_close_func=lambda w: w.workspace.shutdown())
    other.show()
    activated = []
    monkeypatch.setattr(other, "activateWindow", lambda: activated.append(True))
    cycle_window(view, 1)
    assert activated == [True]


def test_terminal_profile_is_transported_and_saved(tmp_path):
    store = Store(tmp_path / "launcher.db")
    store.save("presets", {"id": "project", "name": "Example", "environment_id": "local", "path": str(tmp_path)})
    sid = LauncherService(store).start("launch", preset_id="project", mode="shell", terminal_profile="cmd", spawn=lambda *a: None)
    job = json.loads(next((tmp_path / "jobs").glob("*.json")).read_text(encoding="utf-8"))
    assert job["request"]["terminal_profile"]["command"] == ["cmd", "/K"]
    assert store.get("sessions", sid)["terminal_profile"] == "cmd"
    record = store.save("workspaces", {"name": "Example", "slots": [{"preset_id": "project", "kind": "shell", "terminal_profile": "cmd"}]})
    assert record["slots"][0]["terminal_profile"] == "cmd"


def test_plugins_validate_arguments_platforms_and_profile_isolation(tmp_path, monkeypatch):
    plugin = {"id": "example", "label": "Example CLI", "platforms": ["windows", "linux"],
              "command": ["example-agent", "--message", "hello; echo injected"], "profile_env": "EXAMPLE_AGENT_HOME"}
    settings.save_settings(tmp_path, {**settings.DEFAULT_SETTINGS, "plugins": [plugin]})
    assert settings.selected_profile(tmp_path, {"kind": "local"}, "example") == plugin
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: name)
    assert runtime.shell_command(plugin) == plugin["command"]
    for changed in ({"command": "example-agent"}, {"profile_env": "HOME"}, {"command": ["./project-agent"]}, {"label": "a\nb"}):
        with pytest.raises(ValueError):
            runtime.validate_terminal_profile({**plugin, **changed})
    with pytest.raises(ValueError):
        runtime.validate_request({"action": "login", "mode": "shell", "terminal_profile": plugin})
    with pytest.raises(ValueError):
        settings.validate_settings({**settings.DEFAULT_SETTINGS, "plugins": [{k: v for k, v in plugin.items() if k != "profile_env"}]})
    monkeypatch.setattr(runtime, "WINDOWS", False)
    with pytest.raises(ValueError, match="incompatible"):
        runtime.shell_command(settings.BUILTIN_PROFILES[1])


def test_plugin_worker_uses_private_profile_and_preserves_argument_boundaries(tmp_path, monkeypatch):
    profile = {"id": "example", "label": "Example CLI", "platforms": ["windows", "linux"],
               "command": ["example-agent", "a; echo b"], "profile_env": "EXAMPLE_AGENT_HOME"}
    launches = []

    class Process:
        pid = 123
        def poll(self):
            return 0
        def wait(self):
            return 0

    monkeypatch.setattr(runtime, "trusted_executable", lambda name: name)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda args, **kw: launches.append((args, kw)) or Process())
    monkeypatch.setattr(runtime, "save_status", lambda *args, **kw: None)
    runtime.run_worker({"action": "worker", "mode": "shell", "session_id": "session", "path": str(tmp_path),
                        "terminal_profile": profile}, tmp_path, env={"PATH": "keep", "CODEX_HOME": "drop", "OPENAI_API_KEY": "example"})
    args, kwargs = launches[0]
    assert args == ["example-agent", "a; echo b"] and "shell" not in kwargs
    assert kwargs["env"] == {"PATH": "keep", "EXAMPLE_AGENT_HOME": str(tmp_path / "profiles" / "plugins" / "example")}
    assert (tmp_path / "profiles" / "plugins" / "example").is_dir()
