"""The quiet colour coding: every area / service has a hue that exists in every theme and stays a faint tint."""
import pytest

from vibe_launcher import icons, theme
from vibe_launcher.kit import ICON_ROLE, NavButton, PageHeader, make_button
from vibe_launcher.theme import SECTION_HUES, TINT_A, TINT_STRONG_A, TOOL_HUES, TOOL_ICONS, resolve


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
