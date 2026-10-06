"""Themes: complete readable palettes, a custom accent, saved preferences, and a live switch of the whole window."""
import json

import pytest
from PySide6.QtWidgets import QPushButton

from vibe_launcher import terminal, theme
from vibe_launcher.appearance import AppearanceDialog
from vibe_launcher.kit import ElidedLabel
from vibe_launcher.prefs import UiPrefs
from vibe_launcher.store import Store
from vibe_launcher.theme import C, THEMES, TONES, contrast
from vibe_launcher.ui import MainWindow

from test_env_ui import FakeService

SURFACES = ("bg", "surface", "sidebar", "overlay", "surface_sunken", "surface_hover")
READABLE = ("text", "text_secondary", "text_muted", "success", "warning", "danger", "info", "neutral", "accent")


@pytest.fixture(autouse=True)
def default_theme_afterwards():
    yield
    theme.apply_theme(theme.DEFAULT_THEME)
    theme.set_fonts()
    terminal.configure(font_size=14, cursor_blink=False)


@pytest.mark.parametrize("theme_id", list(THEMES))
def test_every_theme_is_complete_and_readable(theme_id):
    palette = THEMES[theme_id]
    assert len(palette.ansi) == 16
    for fg in READABLE:
        for bg in SURFACES:
            ratio = contrast(getattr(palette, fg), getattr(palette, bg))
            assert ratio >= 4.5, f"{theme_id}: {fg} on {bg} is {ratio:.2f}:1"
    theme.apply_theme(theme_id)
    assert contrast(C.accent_ink, C.accent) >= 4.5
    assert TONES["ok"] == palette.success and TONES["accent"] == palette.accent
    sheet = theme.build_stylesheet()
    assert palette.bg in sheet and palette.surface_raised in sheet
    xterm = theme.terminal_theme()
    assert xterm["background"] == palette.bg and xterm["brightWhite"] == palette.ansi[15]
    assert xterm["selectionBackground"].startswith("rgba(") and "0.28)" in xterm["selectionBackground"], "CSS alpha is 0..1"


@pytest.mark.parametrize("accent", ["#FFFF00", "#1E3A8A", "#888888"])
def test_a_custom_accent_gets_readable_ink_and_deeper_hover(accent):
    theme.apply_theme("nord", accent)
    assert C.accent == accent and C.theme_id == "nord" and C.bg == THEMES["nord"].bg
    assert contrast(C.accent_ink, C.accent) >= 4.5
    assert theme.luminance(C.accent_hover) < theme.luminance(C.accent)
    theme.apply_theme("nord", "not a colour")
    assert C.accent == THEMES["nord"].accent


def test_unknown_theme_falls_back_to_the_default():
    assert theme.apply_theme("no-such-theme").id == theme.DEFAULT_THEME


def test_token_names_resolve_in_the_active_theme():
    theme.apply_theme("github-light")
    assert theme.resolve("text_muted") == THEMES["github-light"].text_muted
    assert theme.resolve("#123456") == "#123456" and theme.resolve(None) is None


def test_appearance_preferences_round_trip_and_reject_junk(tmp_path):
    prefs = UiPrefs(tmp_path / "ui-state.json")
    assert (prefs.theme, prefs.accent, prefs.terminal_font_size, prefs.cursor_blink, prefs.composer_default) == \
        ("deep-sea", "", 14, False, True)
    prefs.theme, prefs.accent, prefs.terminal_font_size, prefs.cursor_blink, prefs.composer_default = \
        "dracula", "#F472B6", 17, True, False
    prefs.save()
    again = UiPrefs(tmp_path / "ui-state.json")
    assert (again.theme, again.accent, again.terminal_font_size, again.cursor_blink, again.composer_default) == \
        ("dracula", "#F472B6", 17, True, False)
    (tmp_path / "ui-state.json").write_text(json.dumps({"theme": "evil", "accent": "red; x", "terminal_font_size": 999,
                                                        "cursor_blink": "yes"}), encoding="utf-8")
    junk = UiPrefs(tmp_path / "ui-state.json")
    assert (junk.theme, junk.accent, junk.terminal_font_size, junk.cursor_blink) == ("deep-sea", "", 14, False)


# ----------------------------------------------------------------------------- the window


@pytest.fixture
def view(tmp_path, qtbot):
    window = MainWindow(Store(tmp_path / "gui.db"), FakeService())
    qtbot.addWidget(window)
    window.show()
    return window


def test_switching_theme_restyles_the_window_live_and_is_remembered(view, tmp_path):
    label = ElidedLabel("x", color="text_muted")
    view.apply_appearance({"theme": "solarized-light", "accent": "#A78BFA", "terminal_font_size": 16})
    assert C.theme_id == "solarized-light" and C.accent == "#A78BFA"
    assert THEMES["solarized-light"].bg in view.styleSheet()
    assert theme.resolve(label._color) == THEMES["solarized-light"].text_muted
    assert terminal.TerminalPrefs.font_size == 16
    saved = json.loads((tmp_path / "ui-state.json").read_text(encoding="utf-8"))
    assert (saved["theme"], saved["accent"], saved["terminal_font_size"]) == ("solarized-light", "#A78BFA", 16)
    buttons = [b for b in view.findChildren(QPushButton) if b.property("vlIcon")]
    assert buttons and all(not b.icon().isNull() for b in buttons)


def test_new_windows_start_in_the_saved_theme(tmp_path, qtbot):
    prefs = UiPrefs(tmp_path / "ui-state.json")
    prefs.theme = "tokyo-night"
    prefs.save()
    window = MainWindow(Store(tmp_path / "gui.db"), FakeService())
    qtbot.addWidget(window)
    assert C.theme_id == "tokyo-night" and THEMES["tokyo-night"].bg in window.styleSheet()


def test_the_dialog_previews_live_and_cancel_puts_everything_back(view, qtbot):
    before = view.appearance_settings()
    dialog = AppearanceDialog(view, before)
    dialog.changed.connect(lambda s: view.apply_appearance(s, save=False))
    dialog.cards["dracula"].click()
    assert C.theme_id == "dracula"
    dialog.swatches[3].click()
    assert C.accent == theme.ACCENT_PRESETS[3]
    dialog.font_size.setValue(18)
    assert terminal.TerminalPrefs.font_size == 18
    dialog.reject()
    assert C.theme_id == before["theme"] and C.accent == THEMES[before["theme"]].accent
    assert terminal.TerminalPrefs.font_size == before["terminal_font_size"]


def test_the_palette_offers_appearance_and_each_theme(view):
    commands = {c.id: c for c in view._commands()}
    assert "appearance" in commands and all(f"theme-{t}" in commands for t in THEMES)
    assert not commands[f"theme-{theme.DEFAULT_THEME}"].enabled, "the current theme is shown as in use"
    commands["theme-nord"].callback()
    assert C.theme_id == "nord" and view.prefs.theme == "nord"


def test_font_preferences_are_validated_and_remembered(tmp_path):
    prefs = UiPrefs(tmp_path / "ui-state.json")
    assert (prefs.ui_font, prefs.terminal_font) == ("", "")
    prefs.ui_font, prefs.terminal_font = "Segoe UI", "Cascadia Mono"
    prefs.save()
    again = UiPrefs(tmp_path / "ui-state.json")
    assert (again.ui_font, again.terminal_font) == ("Segoe UI", "Cascadia Mono")
    (tmp_path / "ui-state.json").write_text(json.dumps({"ui_font": 5, "terminal_font": 'x"; y{}'}), encoding="utf-8")
    junk = UiPrefs(tmp_path / "ui-state.json")
    assert (junk.ui_font, junk.terminal_font) == ("", "")


def test_choosing_fonts_changes_the_window_and_every_terminal_live(view, tmp_path):
    label = ElidedLabel("x", color="text_muted")
    label.setFont(theme.font())
    mono = ElidedLabel("y", color="text_muted")
    mono.setFont(theme.font(mono=True))
    view.apply_appearance({"ui_font": "Test Sans", "terminal_font": "Test Mono"})
    assert theme.font().families()[0] == "Test Sans" and theme.font(mono=True).families()[0] == "Test Mono"
    assert label.font().families()[0] == "Test Sans" and mono.font().families()[0] == "Test Mono"
    assert theme.terminal_font_stack().startswith('"Test Mono",')
    assert '\\"Test Mono\\"' in terminal.terminal_html()        # inside a JS string literal
    saved = json.loads((tmp_path / "ui-state.json").read_text(encoding="utf-8"))
    assert (saved["ui_font"], saved["terminal_font"]) == ("Test Sans", "Test Mono")
    view.apply_appearance({"ui_font": "", "terminal_font": ""})
    assert label.font().families()[0] == theme.FONT_FAMILIES[0] and "Test" not in theme.terminal_font_stack()


def test_the_appearance_dialog_offers_the_default_and_cancel_restores_fonts(view):
    view.apply_appearance({"ui_font": "Test Sans"}, save=False)
    dialog = AppearanceDialog(view, view.appearance_settings())
    dialog.changed.connect(lambda s: view.apply_appearance(s, save=False))
    assert dialog.ui_font.itemData(0) == "" and dialog.terminal_font.itemData(0) == ""
    assert dialog.ui_font.currentData() == "Test Sans", "a font that is not installed stays selected, not dropped"
    dialog.ui_font.setCurrentIndex(0)
    assert view.prefs.ui_font == "" and theme.font().families()[0] == theme.FONT_FAMILIES[0]
    dialog.reject()
    assert view.prefs.ui_font == "Test Sans" and theme.font().families()[0] == "Test Sans"


def test_font_drop_down_opens_with_a_search_field(qtbot):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLineEdit, QListWidget
    from vibe_launcher.appearance import SearchableCombo
    combo = SearchableCombo()
    qtbot.addWidget(combo)
    for name in ("Default", "Alpha Mono", "Beta Sans", "Gamma Mono"):
        combo.addItem(name, name)
    combo.show()
    combo.showPopup()
    popup = combo._popup
    search, listing = popup.findChild(QLineEdit), popup.findChild(QListWidget)
    assert popup.isVisible() and search.isVisible()
    search.setText("mono")
    assert [listing.item(i).text() for i in range(listing.count()) if not listing.item(i).isHidden()] == ["Alpha Mono", "Gamma Mono"]
    assert listing.currentItem().text() == "Alpha Mono"
    qtbot.keyClick(search, Qt.Key.Key_Down)
    qtbot.keyClick(search, Qt.Key.Key_Return)
    assert combo.currentText() == "Gamma Mono" and not popup.isVisible()
