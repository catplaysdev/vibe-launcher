"""Themes, colour coding and project icons.

- theme: Themes: complete readable palettes, a custom accent, saved preferences, and a live switch of the whole window.
- color coding: The quiet colour coding: every area / service has a hue that exists in every theme and stays a faint tint.
- project icons: Project icons (glyph / picture / recoloured SVG), service colours in Quick open, and the project page drawer.
"""
import json
from test_environments import FakeService
from test_workspace import FakeTerminal, SpawnService

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QDialog, QPushButton
from vibe_launcher import icons, project_icons, terminal, theme
from vibe_launcher.appearance import AppearanceDialog
from vibe_launcher.kit import ICON_ROLE, ElidedLabel, make_button, NavButton, PageHeader
from vibe_launcher.prefs import UiPrefs
from vibe_launcher.project_icon_dialog import ProjectIconDialog
from vibe_launcher.store import Store
from vibe_launcher.theme import (C, SECTION_HUES, THEMES, TINT_A, TINT_STRONG_A, TONES, TOOL_HUES, TOOL_ICONS,
                                 contrast, resolve)
from vibe_launcher.ui import MainWindow


# --- theme ------------------------------------------------------------------------------------------------------------
SURFACES = ("bg", "surface", "sidebar", "overlay", "surface_sunken", "surface_hover")
READABLE = ("text", "text_secondary", "text_muted", "success", "warning", "danger", "info", "neutral", "accent")


@pytest.fixture
def default_theme_afterwards():
    yield
    theme.apply_theme(theme.DEFAULT_THEME)
    theme.set_fonts()
    terminal.configure(font_size=14, cursor_blink=False)


@pytest.mark.usefixtures("default_theme_afterwards")
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


@pytest.mark.usefixtures("default_theme_afterwards")
@pytest.mark.parametrize("accent", ["#FFFF00", "#1E3A8A", "#888888"])
def test_a_custom_accent_gets_readable_ink_and_deeper_hover(accent):
    theme.apply_theme("nord", accent)
    assert C.accent == accent and C.theme_id == "nord" and C.bg == THEMES["nord"].bg
    assert contrast(C.accent_ink, C.accent) >= 4.5
    assert theme.luminance(C.accent_hover) < theme.luminance(C.accent)
    theme.apply_theme("nord", "not a colour")
    assert C.accent == THEMES["nord"].accent


@pytest.mark.usefixtures("default_theme_afterwards")
def test_unknown_theme_falls_back_to_the_default():
    assert theme.apply_theme("no-such-theme").id == theme.DEFAULT_THEME


@pytest.mark.usefixtures("default_theme_afterwards")
def test_token_names_resolve_in_the_active_theme():
    theme.apply_theme("github-light")
    assert theme.resolve("text_muted") == THEMES["github-light"].text_muted
    assert theme.resolve("#123456") == "#123456" and theme.resolve(None) is None


@pytest.mark.usefixtures("default_theme_afterwards")
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


@pytest.mark.usefixtures("default_theme_afterwards")
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


@pytest.mark.usefixtures("default_theme_afterwards")
def test_new_windows_start_in_the_saved_theme(tmp_path, qtbot):
    prefs = UiPrefs(tmp_path / "ui-state.json")
    prefs.theme = "tokyo-night"
    prefs.save()
    window = MainWindow(Store(tmp_path / "gui.db"), FakeService())
    qtbot.addWidget(window)
    assert C.theme_id == "tokyo-night" and THEMES["tokyo-night"].bg in window.styleSheet()


@pytest.mark.usefixtures("default_theme_afterwards")
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


@pytest.mark.usefixtures("default_theme_afterwards")
def test_the_palette_offers_appearance_and_each_theme(view):
    commands = {c.id: c for c in view._commands()}
    assert "appearance" in commands and all(f"theme-{t}" in commands for t in THEMES)
    assert not commands[f"theme-{theme.DEFAULT_THEME}"].enabled, "the current theme is shown as in use"
    commands["theme-nord"].callback()
    assert C.theme_id == "nord" and view.prefs.theme == "nord"


@pytest.mark.usefixtures("default_theme_afterwards")
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


@pytest.mark.usefixtures("default_theme_afterwards")
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


@pytest.mark.usefixtures("default_theme_afterwards")
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


@pytest.mark.usefixtures("default_theme_afterwards")
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


# --- color coding -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("theme_id", sorted(theme.THEMES))
def test_every_hue_token_resolves_to_a_colour_in_every_theme(theme_id):
    theme.apply_theme(theme_id)
    try:
        for token in (*SECTION_HUES.values(), *TOOL_HUES.values()):
            assert theme.valid_color(resolve(token)), (theme_id, token)
    finally:
        theme.apply_theme(theme.DEFAULT_THEME)


def test_tints_stay_faint_so_long_sessions_are_easy_on_the_eyes():
    assert TINT_A <= 0.14 and TINT_STRONG_A <= 0.22


def test_every_area_and_service_has_an_icon_that_exists():
    for icon in TOOL_ICONS.values():
        assert icon in icons.names()
    assert TOOL_ICONS["claude"] == "claude" and TOOL_ICONS["codex"] == "codex"
    assert not icons.pixmap("claude", "#ffffff", 16).toImage().isNull()
    assert set(SECTION_HUES) == {"projects", "workspace", "accounts", "environments", "sessions"}


def test_nav_entries_carry_their_hue_and_utility_buttons_stay_plain(qtbot):
    tinted = NavButton("projects", "專案", "folder", hue="accent")
    plain = NavButton("import", "匯入", "download", checkable=False, muted=True)
    qtbot.addWidget(tinted)
    qtbot.addWidget(plain)
    assert tinted._hue == "accent" and plain._hue == ""
    tinted.grab()
    plain.grab()


def test_page_header_shows_a_tile_only_when_given_an_icon(qtbot):
    with_icon = PageHeader("專案", "x", icon="folder", hue="accent")
    without = PageHeader("專案", "x")
    qtbot.addWidget(with_icon)
    qtbot.addWidget(without)
    assert with_icon.tile is not None and without.tile is None


def test_a_tinted_button_icon_follows_the_theme(qtbot):
    button = make_button("Codex", "subtle", "square-terminal", small=True, tint="info")
    qtbot.addWidget(button)
    assert button.property("vlTint") == "info" and not button.icon().isNull()
    assert ICON_ROLE != 0


def test_brand_marks_come_from_local_files_with_generic_fallback(tmp_path, monkeypatch):
    from vibe_launcher import icons
    monkeypatch.setattr(icons, "BRAND_DIR", tmp_path)
    monkeypatch.setattr(icons, "_brands", {})
    monkeypatch.setattr(icons, "_renderers", {})
    monkeypatch.setattr(icons, "_cache", {})
    assert icons._brand("claude") is None                       # not shipped: the generic mark stands in
    assert not icons.pixmap("claude", "#ffffff", 16).toImage().isNull()
    (tmp_path / "deepseek.svg").write_text(
        '<svg viewBox="0 0 40 20"><style>.a{fill-rule:evenodd}</style><path class="a" d="M0 0h40v20H0z"/></svg>', encoding="utf-8")
    box, body = icons._brand("deepseek")
    assert box == "0 -10 40 40" and 'fill-rule="evenodd"' in body


def test_logo_eyes_follow_the_theme_accent():
    from vibe_launcher import icons
    image = icons.logo_pixmap(128, "#000000", "#FF0000").toImage()
    colours = {image.pixelColor(x, y).name() for x in range(128) for y in range(128) if image.pixelColor(x, y).alpha() == 255}
    assert "#ff0000" in colours and "#000000" in colours and "#10bfea" not in colours
    plate = icons.logo_pixmap(64, icons.LOGO_LINE, "#00ff00", plate=icons.LOGO_PLATE).toImage()
    assert plate.pixelColor(32, 4).name() == icons.LOGO_PLATE.lower()   # the cream plate behind the mark
    assert plate.pixelColor(1, 1).alpha() == 0                          # ...is round: the corners stay clear
    assert not icons.logo_icon("#00ff00").isNull()


# --- project icons ----------------------------------------------------------------------------------------------------
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#000"/></svg>'


@pytest.fixture
def icons_view(tmp_path, qtbot):
    store = Store(tmp_path / "launcher.db")
    store.save("presets", {"id": "p1", "name": "Site analysis", "environment_id": "local", "path": str(tmp_path),
                           "last_used": "2026-10-02T00:00:00+00:00"})
    store.save("presets", {"id": "p2", "name": "Frontend", "environment_id": "local", "path": str(tmp_path)})
    window = MainWindow(store, SpawnService(), terminal_factory=FakeTerminal)
    qtbot.addWidget(window, before_close_func=lambda w: w.workspace.shutdown())
    window.show()
    window.show_page("projects")
    yield window
    project_icons.configure(tmp_path, {})


def choose(monkeypatch, act):
    """Run the icon dialog with ``act(dialog)`` instead of user clicks; returns what exec answers."""
    def fake_exec(dialog):
        return act(dialog)
    monkeypatch.setattr(ProjectIconDialog, "exec", fake_exec)


def select(icons_view, project_id):
    for row in range(icons_view.project_list.count()):
        if icons_view.project_list.item(row).data(0x0100) == project_id:
            icons_view.project_list.setCurrentRow(row)
    assert icons_view._selected_project == project_id


def test_entries_are_validated():
    assert project_icons.valid_entry({"kind": "glyph", "name": "cpu", "color": "#abcdef"}) == \
        {"kind": "glyph", "name": "cpu", "color": "#ABCDEF"}
    assert project_icons.valid_entry({"kind": "glyph", "name": "../x"}) is None
    assert project_icons.valid_entry({"kind": "file", "name": "../../evil.svg"}) is None
    assert project_icons.valid_entry({"kind": "file", "name": "a" * 32 + ".exe"}) is None
    assert project_icons.valid_entries({"p": {"kind": "glyph", "name": "star"}, "q": "bad", 3: {}}) == \
        {"p": {"kind": "glyph", "name": "star", "color": ""}}


def test_svg_recolour_keeps_none_and_paints_unfilled_shapes(qapp):
    text = project_icons.recolor_svg('<svg viewBox="0 0 1 1"><path fill="none" stroke="#123456"/>'
                                     '<path style="fill:red;opacity:1"/><path/></svg>', "#FF00AA")
    assert 'fill="none"' in text and 'stroke="#FF00AA"' in text and "fill:#FF00AA" in text
    assert text.startswith('<svg fill="#FF00AA"')
    picture = project_icons.render(SVG, ".svg", 20, 1.0, "#FF0000").toImage()
    assert picture.pixelColor(10, 10).name() == "#ff0000"


def test_pictures_are_checked_and_copied_privately(tmp_path, qapp):
    with pytest.raises(ValueError):
        project_icons.check_picture(SVG, ".exe")
    with pytest.raises(ValueError):
        project_icons.check_picture(b"not a picture", ".png")
    with pytest.raises(ValueError):
        project_icons.check_picture(b"x" * (project_icons.MAX_BYTES + 1), ".svg")
    name = project_icons.store_picture(tmp_path, SVG, ".svg")
    stored = tmp_path / "project-icons" / name
    assert stored.read_bytes() == SVG and project_icons.valid_entry({"kind": "file", "name": name})
    project_icons.remove_picture(tmp_path, name)
    assert not stored.exists()
    project_icons.remove_picture(tmp_path, "../launcher.db")       # never outside the icon folder


def test_choose_svg_with_colour_then_glyph_then_default(icons_view, tmp_path, monkeypatch):
    source = tmp_path / "logo.svg"
    source.write_bytes(SVG)
    select(icons_view, "p1")

    def pick_svg(dialog):
        dialog._pick_file = lambda: str(source)
        dialog.pick()
        dialog.choose_colour("#F472B6")
        return QDialog.DialogCode.Accepted
    choose(monkeypatch, pick_svg)
    icons_view.change_project_icon()
    entry = icons_view.prefs.project_icons["p1"]
    assert entry["kind"] == "file" and entry["color"] == "#F472B6"
    stored = tmp_path / "project-icons" / entry["name"]
    assert stored.exists()
    assert json.loads(icons_view.prefs.path.read_text(encoding="utf-8"))["project_icons"]["p1"] == entry
    assert icons_view.project_list.currentItem().data(0x0100 + 5) == project_icons.signature("p1")
    tile = icons_view.project_tile.grab().toImage()
    colours = {tile.pixelColor(x, y).name() for x in range(tile.width()) for y in range(tile.height())}
    assert "#f472b6" in colours                                    # the recoloured SVG is what the header shows

    choose(monkeypatch, lambda d: (d.choose_glyph("cpu"), QDialog.DialogCode.Accepted)[1])
    icons_view.change_project_icon()
    assert icons_view.prefs.project_icons["p1"] == {"kind": "glyph", "name": "cpu", "color": "#F472B6"}
    assert not stored.exists()                                     # the replaced picture is deleted

    choose(monkeypatch, lambda d: (d.reset(), QDialog.DialogCode.Accepted)[1])
    icons_view.change_project_icon()
    assert "p1" not in icons_view.prefs.project_icons


def test_cancel_changes_nothing_and_photos_take_no_colour(icons_view, tmp_path, monkeypatch):
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("#00FF00"))
    photo = tmp_path / "photo.png"
    image.save(str(photo))
    select(icons_view, "p2")

    def pick_then_cancel(dialog):
        dialog._pick_file = lambda: str(photo)
        dialog.pick()
        dialog.choose_colour("#FF0000")              # ignored: a photo keeps its colours
        assert dialog.entry["color"] == "" and not dialog.default_swatch.isEnabled()
        return QDialog.DialogCode.Rejected
    choose(monkeypatch, pick_then_cancel)
    icons_view.change_project_icon()
    assert "p2" not in icons_view.prefs.project_icons and not (tmp_path / "project-icons").exists()


def test_bad_picture_is_refused_inside_the_dialog(icons_view, tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a picture")
    dialog = ProjectIconDialog(icons_view, "Frontend", None)
    dialog._pick_file = lambda: str(bad)
    dialog.pick()
    assert dialog.pending is None and dialog.error.isVisibleTo(dialog) and dialog.error.text()
    dialog.reject()


def test_deleted_project_forgets_its_icon_and_picture(icons_view, tmp_path):
    name = project_icons.store_picture(tmp_path, SVG, ".svg")
    icons_view.prefs.project_icons["p2"] = {"kind": "file", "name": name, "color": ""}
    icons_view.store.delete("presets", "p2")
    icons_view.reload()
    assert "p2" not in icons_view.prefs.project_icons and not (tmp_path / "project-icons" / name).exists()


def test_home_shows_the_project_icon_not_the_account_tool(icons_view):
    icons_view.prefs.project_icons["p1"] = {"kind": "glyph", "name": "cpu", "color": ""}
    icons_view._refresh_project_icons()
    tiles = [t for t in icons_view.home_page.findChildren(type(icons_view.project_tile).__mro__[1]) if t._project]
    assert {t._project for t in tiles} >= {"p1"}


def test_service_colours_are_their_own(qapp):
    for theme_id in theme.THEMES:
        theme.apply_theme(theme_id)
        claude, terminal = theme.resolve(theme.tool_hue("claude")), theme.resolve(theme.tool_hue("shell"))
        assert claude not in {theme.C.warning, theme.C.accent} and terminal not in {theme.C.warning, theme.C.accent}
        assert theme.contrast(claude, theme.C.surface) >= 3 and theme.contrast(terminal, theme.C.surface) >= 3
    theme.apply_theme(theme.DEFAULT_THEME)


def test_drawer_is_a_plain_section(icons_view):
    from vibe_launcher.kit import IconButton
    from vibe_launcher.work_drawer import WorkDrawer
    assert "paintEvent" not in WorkDrawer.__dict__                 # no tinted frame around the cards
    assert isinstance(icons_view.work_drawer.add, IconButton) and icons_view.work_drawer.add.accessibleName()


def test_clicking_the_icon_in_the_project_list_opens_the_picker(icons_view, monkeypatch, qtbot):
    from PySide6.QtCore import QPoint, Qt
    opened = []
    choose(monkeypatch, lambda d: (opened.append(icons_view._selected_project), QDialog.DialogCode.Rejected)[1])
    listing = icons_view.project_list
    row = next(r for r in range(listing.count()) if listing.item(r).data(0x0100) == "p2")
    rect = listing.visualItemRect(listing.item(row))
    qtbot.mouseClick(listing.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(rect.left() + 28, rect.center().y()))
    qtbot.waitUntil(lambda: opened == ["p2"], timeout=2000)                    # the icon: picker opens for that row
    opened.clear()
    qtbot.mouseClick(listing.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(rect.left() + 120, rect.center().y()))
    qtbot.wait(100)
    assert opened == []                                                         # the rest of the row only selects


def test_workspace_tiles_use_their_own_default_glyph_and_hue(qapp):
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QPainter

    def tile(**kwargs):
        image = QImage(64, 64, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        project_icons.draw(painter, QRectF(0, 0, 64, 64), entry={}, dpr=1.0, **kwargs)
        painter.end()
        return image
    assert tile(glyph="briefcase", hue="info") != tile()
    assert not project_icons.pixmap(None, 18, hue="info", glyph="briefcase").isNull()


def test_the_name_field_refuses_empty_and_taken_names(icons_view, qtbot):
    dialog = ProjectIconDialog(icons_view, "Site", None, title="Workspace", hue="info", glyph="briefcase",
                               name_label="Name", taken={"Data"})
    qtbot.addWidget(dialog)
    dialog.name_field.setText("  ")
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted and dialog.error.isVisibleTo(dialog)
    dialog.name_field.setText("data")
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    dialog.name_field.setText("  Site   work ")
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted and dialog.result_name() == "Site work"
