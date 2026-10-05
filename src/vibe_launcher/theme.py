"""Design tokens and the application stylesheet.

Every colour, radius, spacing step and type size used by the UI lives here.
Widgets read tokens from this module (or use the stylesheet) instead of
hard-coding values, so the look can be tuned in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QFont

# --------------------------------------------------------------------------- colour


def rgba(hex_color: str, alpha: float) -> str:
    """QSS ``rgba()`` string for ``#rrggbb`` at ``alpha`` (0..1). QSS takes the alpha as 0..255."""
    c = QColor(hex_color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {round(alpha * 255)})"


def css_rgba(hex_color: str, alpha: float) -> str:
    """CSS ``rgba()`` string (for the terminal page). Unlike QSS, CSS takes the alpha as 0..1."""
    c = QColor(hex_color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha:g})"


def qcolor(hex_color: str, alpha: float = 1.0) -> QColor:
    c = QColor(hex_color)
    c.setAlphaF(alpha)
    return c


def luminance(hex_color: str) -> float:
    """WCAG relative luminance."""
    c = QColor(hex_color)

    def channel(v: int) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(c.red()) + 0.7152 * channel(c.green()) + 0.0722 * channel(c.blue())


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two colours (1..21)."""
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def valid_color(value: object) -> str:
    """``value`` if it is a ``#rrggbb`` colour, else ""."""
    if isinstance(value, str) and len(value) == 7 and value.startswith("#"):
        try:
            int(value[1:], 16)
            return value.upper()
        except ValueError:
            pass
    return ""


ANSI_NAMES = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
              "brightBlack", "brightRed", "brightGreen", "brightYellow", "brightBlue", "brightMagenta", "brightCyan",
              "brightWhite")


@dataclass(frozen=True)
class Theme:
    """A complete palette: the app's colour tokens plus the terminal's 16 ANSI colours."""

    id: str
    name: str
    dark: bool
    bg: str
    sidebar: str
    surface: str
    surface_hover: str
    surface_raised: str                 # button hover
    surface_pressed: str
    surface_sunken: str                 # inputs, wells
    overlay: str                        # menus, dialogs, drawer
    border: str                         # used with alpha, see BORDER_A
    text: str
    text_secondary: str
    text_muted: str                     # small metadata; keeps >= 4.5:1 on every surface
    text_disabled: str
    accent: str
    success: str
    warning: str
    danger: str
    info: str                           # informational / in progress (not a warning)
    neutral: str                        # unknown / disabled
    ansi: tuple[str, ...]               # 16 colours, in ANSI_NAMES order
    accent_hover: str = ""              # derived from ``accent`` when empty
    accent_pressed: str = ""
    accent_ink: str = ""                # text on a solid accent fill


THEMES: dict[str, Theme] = {t.id: t for t in (
    Theme("deep-sea", "深海", True,
          bg="#0B1120", sidebar="#101827", surface="#151F32", surface_hover="#1B2940", surface_raised="#223350",
          surface_pressed="#1A2840", surface_sunken="#0E1626", overlay="#101A2C", border="#94A3B8",
          text="#F1F5F9", text_secondary="#94A3B8", text_muted="#8190A8", text_disabled="#55627A",
          accent="#5EEAD4", success="#4ADE80", warning="#FBBF24", danger="#F87171", info="#60A5FA", neutral="#94A3B8",
          accent_hover="#2DD4BF", accent_pressed="#22B8A6", accent_ink="#06231F",
          ansi=("#1E293B", "#F87171", "#4ADE80", "#FBBF24", "#60A5FA", "#C4B5FD", "#5EEAD4", "#CBD5E1",
                "#64748B", "#FCA5A5", "#86EFAC", "#FDE68A", "#93C5FD", "#DDD6FE", "#99F6E4", "#F1F5F9")),
    Theme("one-dark", "One Dark", True,
          bg="#21252B", sidebar="#1C1F24", surface="#282C34", surface_hover="#2F343E", surface_raised="#3A404B",
          surface_pressed="#30353F", surface_sunken="#1D2025", overlay="#252931", border="#ABB2BF",
          text="#E6E9EF", text_secondary="#ABB2BF", text_muted="#9AA1AE", text_disabled="#5C6370",
          accent="#61AFEF", success="#98C379", warning="#E5C07B", danger="#EE8A92", info="#56B6C2", neutral="#9DA5B4",
          ansi=("#3F4451", "#E06C75", "#98C379", "#E5C07B", "#61AFEF", "#C678DD", "#56B6C2", "#D7DAE0",
                "#5C6370", "#EA858B", "#B0D196", "#F0D197", "#8CC4F5", "#D79CE8", "#7BCBD5", "#FFFFFF")),
    Theme("dracula", "Dracula", True,
          bg="#282A36", sidebar="#21222C", surface="#2F3140", surface_hover="#383A4C", surface_raised="#44475A",
          surface_pressed="#363848", surface_sunken="#1E1F29", overlay="#2A2C39", border="#A9AFC9",
          text="#F8F8F2", text_secondary="#C3C7DC", text_muted="#AEB3CC", text_disabled="#6272A4",
          accent="#BD93F9", success="#50FA7B", warning="#F1FA8C", danger="#FF8A8A", info="#8BE9FD", neutral="#A9AFC9",
          ansi=("#21222C", "#FF5555", "#50FA7B", "#F1FA8C", "#BD93F9", "#FF79C6", "#8BE9FD", "#F8F8F2",
                "#6272A4", "#FF6E6E", "#69FF94", "#FFFFA5", "#D6ACFF", "#FF92DF", "#A4FFFF", "#FFFFFF")),
    Theme("nord", "Nord", True,
          bg="#2E3440", sidebar="#2A303B", surface="#3B4252", surface_hover="#434C5E", surface_raised="#4C566A",
          surface_pressed="#3F4757", surface_sunken="#272C36", overlay="#323946", border="#D8DEE9",
          text="#ECEFF4", text_secondary="#D8DEE9", text_muted="#BCC4D2", text_disabled="#6B768A",
          accent="#8FC8D8", success="#B4CF9E", warning="#EBCB8B", danger="#F0AAB0", info="#A6C2E0", neutral="#BCC4D2",
          ansi=("#3B4252", "#BF616A", "#A3BE8C", "#EBCB8B", "#81A1C1", "#B48EAD", "#88C0D0", "#E5E9F0",
                "#4C566A", "#D08770", "#A3BE8C", "#EBCB8B", "#81A1C1", "#B48EAD", "#8FBCBB", "#ECEFF4")),
    Theme("catppuccin", "Catppuccin Mocha", True,
          bg="#1E1E2E", sidebar="#181825", surface="#262637", surface_hover="#313244", surface_raised="#3B3D52",
          surface_pressed="#2D2E40", surface_sunken="#181825", overlay="#232334", border="#A6ADC8",
          text="#CDD6F4", text_secondary="#BAC2DE", text_muted="#A2A8C2", text_disabled="#585B70",
          accent="#CBA6F7", success="#A6E3A1", warning="#F9E2AF", danger="#F38BA8", info="#89B4FA", neutral="#A2A8C2",
          ansi=("#45475A", "#F38BA8", "#A6E3A1", "#F9E2AF", "#89B4FA", "#F5C2E7", "#94E2D5", "#BAC2DE",
                "#585B70", "#F38BA8", "#A6E3A1", "#F9E2AF", "#89B4FA", "#F5C2E7", "#94E2D5", "#A6ADC8")),
    Theme("tokyo-night", "Tokyo Night", True,
          bg="#1A1B26", sidebar="#16161E", surface="#222436", surface_hover="#292E42", surface_raised="#343A55",
          surface_pressed="#2A2F45", surface_sunken="#15161F", overlay="#1F2030", border="#A9B1D6",
          text="#C0CAF5", text_secondary="#A9B1D6", text_muted="#959DC4", text_disabled="#565F89",
          accent="#7AA2F7", success="#9ECE6A", warning="#E0AF68", danger="#F7768E", info="#7DCFFF", neutral="#959DC4",
          ansi=("#15161E", "#F7768E", "#9ECE6A", "#E0AF68", "#7AA2F7", "#BB9AF7", "#7DCFFF", "#A9B1D6",
                "#414868", "#F7768E", "#9ECE6A", "#E0AF68", "#7AA2F7", "#BB9AF7", "#7DCFFF", "#C0CAF5")),
    Theme("github-light", "GitHub Light", False,
          bg="#F6F8FA", sidebar="#EEF1F4", surface="#FFFFFF", surface_hover="#EEF1F4", surface_raised="#E3E7EC",
          surface_pressed="#D9DEE4", surface_sunken="#FFFFFF", overlay="#FFFFFF", border="#57606A",
          text="#1F2328", text_secondary="#4C545D", text_muted="#565E67", text_disabled="#A4ACB5",
          accent="#0969DA", success="#16712F", warning="#8A5C00", danger="#CF222E", info="#0969DA", neutral="#565E67",
          ansi=("#24292F", "#CF222E", "#116329", "#4D2D00", "#0969DA", "#8250DF", "#1B7C83", "#6E7781",
                "#57606A", "#A40E26", "#1A7F37", "#633C01", "#218BFF", "#A475F9", "#3192AA", "#8C959F")),
    Theme("solarized-light", "Solarized Light", False,
          bg="#FDF6E3", sidebar="#F4EDD8", surface="#FFFBF0", surface_hover="#F1EAD5", surface_raised="#E9E2CC",
          surface_pressed="#E1DAC4", surface_sunken="#FFFDF7", overlay="#FFFBF0", border="#657B83",
          text="#073642", text_secondary="#43575E", text_muted="#4A5D64", text_disabled="#A3ADAA",
          accent="#1A67A0", success="#566800", warning="#7A5A00", danger="#B52724", info="#1B6399", neutral="#4A5D64",
          ansi=("#073642", "#DC322F", "#859900", "#B58900", "#268BD2", "#D33682", "#2AA198", "#EEE8D5",
                "#002B36", "#CB4B16", "#586E75", "#657B83", "#839496", "#6C71C4", "#93A1A1", "#FDF6E3")),
)}
DEFAULT_THEME = "deep-sea"
TERMINAL_FONT_SIZES = range(10, 23)
ACCENT_PRESETS = ("#5EEAD4", "#60A5FA", "#A78BFA", "#F472B6", "#FB923C", "#FBBF24", "#4ADE80", "#F87171")


class C:
    """Colour tokens of the active theme. ``apply_theme`` rewrites them, so read them when painting, never cache them."""

    theme_id = DEFAULT_THEME
    dark = True
    bg = sidebar = surface = surface_hover = surface_raised = surface_pressed = surface_sunken = overlay = ""
    border = text = text_secondary = text_muted = text_disabled = ""
    accent = accent_hover = accent_pressed = accent_ink = ""
    success = warning = danger = neutral = info = violet = ""
    ansi: tuple[str, ...] = ()


BORDER_A = 0.16
BORDER_STRONG_A = 0.28
ACCENT_SUBTLE_A = 0.10
ACCENT_BORDER_A = 0.45
TONE_BG_A = 0.12

# Semantic status tones -> foreground colour. Filled (and refilled in place) by ``apply_theme``.
TONES: dict[str, str] = {}


# Quiet colour coding: every area of the app has one hue (a token name, so it follows the theme). It only ever appears as a
# low-alpha tile / wash behind an icon, never as a large solid block, which keeps long sessions easy on the eyes.
SECTION_HUES = {"projects": "accent", "workspace": "info", "accounts": "violet", "environments": "warning", "sessions": "success"}
TOOL_HUES = {"codex": "info", "claude": "warning", "deepseek-harness": "violet", "github": "neutral", "shell": "accent"}
TOOL_ICONS = {"codex": "codex", "claude": "claude", "deepseek-harness": "deepseek", "github": "github", "shell": "terminal"}
TINT_A = 0.12            # icon tile / wash
TINT_STRONG_A = 0.20     # the same, for the current item


def section_hue(key: str) -> str:
    return SECTION_HUES.get(key, "accent")


def tool_hue(tool: str) -> str:
    return TOOL_HUES.get(tool, "neutral")


def accent_variants(accent: str) -> tuple[str, str, str]:
    """(hover, pressed, ink) for a solid ``accent`` fill: a little deeper on hover/press; ink with the best contrast."""
    base = QColor(accent)
    hover, pressed = base.darker(112).name().upper(), base.darker(125).name().upper()
    ink = max(("#FFFFFF", "#0B1120"), key=lambda candidate: contrast(accent, candidate))
    return hover, pressed, ink


def apply_theme(theme_id: str = DEFAULT_THEME, accent: str = "") -> Theme:
    """Make ``theme_id`` (optionally with a custom ``accent``) the active palette. Unknown ids fall back to the default.

    This only rewrites the tokens. ``theme_bus().changed`` tells the open UI to repaint (see ``ui.MainWindow``).
    """
    theme = THEMES.get(theme_id) or THEMES[DEFAULT_THEME]
    for name in ("bg", "sidebar", "surface", "surface_hover", "surface_raised", "surface_pressed", "surface_sunken",
                 "overlay", "border", "text", "text_secondary", "text_muted", "text_disabled",
                 "success", "warning", "danger", "info", "neutral", "ansi"):
        setattr(C, name, getattr(theme, name))
    C.theme_id, C.dark = theme.id, theme.dark
    C.violet = theme.ansi[5]
    custom = valid_color(accent)
    if custom and custom != theme.accent.upper():
        C.accent = custom
        C.accent_hover, C.accent_pressed, C.accent_ink = accent_variants(custom)
    else:
        hover, pressed, ink = accent_variants(theme.accent)
        C.accent = theme.accent
        C.accent_hover = theme.accent_hover or hover
        C.accent_pressed = theme.accent_pressed or pressed
        C.accent_ink = theme.accent_ink or ink
    TONES.clear()
    TONES.update(ok=C.success, warn=C.warning, danger=C.danger, unknown=C.neutral, neutral=C.neutral, info=C.info,
                 accent=C.accent)
    return theme


def resolve(color):
    """A colour a widget keeps: a token name ("text_muted") is looked up in the active theme, anything else is as is.

    Widgets store token names rather than hex values so they follow a theme switch without being rebuilt.
    """
    if isinstance(color, str) and color and not color.startswith("#"):
        value = getattr(C, color, None)
        if isinstance(value, str):
            return value
    return color


def terminal_theme() -> dict[str, str]:
    """xterm.js colours for the active palette."""
    return {"background": C.bg, "foreground": C.text, "cursor": C.accent, "cursorAccent": C.bg,
            "selectionBackground": css_rgba(C.accent, 0.28), **dict(zip(ANSI_NAMES, C.ansi))}


def terminal_chrome() -> dict[str, str]:
    """Colours of the terminal page around the text: scrollbar and the input-method composition box."""
    return {"bg": C.bg, "surface": C.surface_hover, "text": C.text, "accent": C.accent,
            "thumb": css_rgba(C.border, 0.28)}


class _ThemeBus(QObject):
    changed = Signal()


_bus: _ThemeBus | None = None


def theme_bus() -> _ThemeBus:
    """``changed`` fires after the active theme changed, for widgets that keep colours of their own."""
    global _bus
    if _bus is None:
        _bus = _ThemeBus()
    return _bus


apply_theme(DEFAULT_THEME)

# --------------------------------------------------------------------------- scale


class S:
    """Spacing scale (px)."""

    xs = 4
    sm = 8
    md = 12
    lg = 16
    xl = 24
    xxl = 32
    huge = 48


class R:
    """Corner radii (px)."""

    sm = 6
    md = 8
    lg = 12
    xl = 16
    pill = 999


class T:
    """Typography scale (px). Deliberately spread so hierarchy reads at a glance."""

    page_title = 26
    detail_title = 20
    dialog_title = 18
    section = 15
    body = 13
    meta = 12
    micro = 11


class M:
    """Motion (ms)."""

    hover = 150
    panel = 200
    sidebar = 200
    toast = 180


class Size:
    control = 36            # button / input height
    hit = 40                # minimum comfortable click target
    sidebar_expanded = 232
    sidebar_medium = 200
    sidebar_rail = 64
    icon = 16
    icon_lg = 20


FONT_FAMILIES = ["Inter", "Microsoft JhengHei UI", "Microsoft JhengHei", "Noto Sans TC", "Segoe UI", "system-ui", "sans-serif"]
MONO_FAMILIES = ["Cascadia Mono", "Consolas", "Microsoft JhengHei UI", "monospace"]


def font(px: int = T.body, weight: QFont.Weight = QFont.Weight.Normal, mono: bool = False) -> QFont:
    f = QFont()
    f.setFamilies(MONO_FAMILIES if mono else FONT_FAMILIES)
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


# --------------------------------------------------------------------------- stylesheet


CHECK_ICON = (Path(__file__).with_name("assets") / "check.svg").as_posix()


def build_stylesheet() -> str:
    border = rgba(C.border, BORDER_A)
    border_strong = rgba(C.border, BORDER_STRONG_A)
    accent_subtle = rgba(C.accent, ACCENT_SUBTLE_A)
    # Fonts are applied with QWidget.setFont() (see ``font``), not with a universal QSS rule:
    # a stylesheet font would override the explicit sizes the custom-painted widgets set.
    return f"""
    * {{ outline: 0; }}
    QMainWindow, QDialog {{ background: {C.bg}; }}
    QWidget {{ color: {C.text}; }}
    QLabel {{ background: transparent; }}
    QLabel[role="muted"] {{ color: {C.text_secondary}; }}
    QLabel[role="meta"] {{ color: {C.text_muted}; font-size: {T.meta}px; }}
    QLabel[role="title"] {{ font-size: {T.page_title}px; font-weight: 600; }}
    QLabel[role="section"] {{ font-size: {T.section}px; font-weight: 600; }}

    QToolTip {{
        background: {C.overlay}; color: {C.text}; border: 1px solid {border_strong};
        border-radius: {R.sm}px; padding: {S.xs + 2}px {S.sm}px; font-size: {T.meta}px;
    }}

    /* ---------- surfaces ---------- */
    QFrame[surface="card"] {{ background: {C.surface}; border-radius: {R.xl}px; border: none; }}
    QFrame[surface="well"] {{ background: {C.surface_sunken}; border-radius: {R.lg}px; border: none; }}

    /* ---------- chrome kept in one place so it follows the theme ---------- */
    QFrame#sidebar {{ background: {C.sidebar}; border: none; }}
    QFrame#divider {{ background: {border}; border: none; }}
    QFrame#menuRule {{ background: {border}; border: none; margin: 4px 8px; }}
    QFrame#menuPanel, QFrame#toast {{ background: {C.overlay}; border: 1px solid {border_strong}; border-radius: {R.lg}px; }}
    QFrame#confirmPanel, QFrame#palettePanel {{
        background: {C.overlay}; border: 1px solid {border_strong}; border-radius: {R.xl}px;
    }}
    QLabel#toastText {{ color: {C.text}; background: transparent; }}

    /* ---------- workspace terminal panes ---------- */
    QFrame#pane {{ background: {C.bg}; border: 1px solid {border}; border-radius: {R.lg}px; }}
    QFrame#pane[active="true"] {{ border: 1px solid {rgba(C.accent, 0.55)}; }}
    QFrame#endedBar {{ background: {rgba(C.border, 0.10)}; border: none; }}
    QFrame#composer {{ background: {C.surface}; border: none; border-top: 1px solid {border};
        border-bottom-left-radius: {R.lg}px; border-bottom-right-radius: {R.lg}px; }}
    QPlainTextEdit#composerEdit {{
        background: {C.surface_sunken}; color: {C.text}; border: 1px solid {border}; border-radius: {R.md}px;
        padding: 2px {S.xs}px; selection-background-color: {rgba(C.accent, 0.35)}; selection-color: {C.text};
    }}
    QPlainTextEdit#composerEdit:focus {{ border: 1px solid {C.accent}; }}

    /* ---------- buttons ---------- */
    QPushButton {{
        background: {C.surface_hover}; color: {C.text}; border: 1px solid transparent;
        border-radius: {R.md}px; padding: 0 {S.lg}px; min-height: {Size.control}px; font-weight: 500;
    }}
    QPushButton:hover {{ background: {C.surface_raised}; }}
    QPushButton:pressed {{ background: {C.surface_pressed}; }}
    QPushButton:focus {{ border: 1px solid {C.accent}; }}
    QPushButton:disabled {{ color: {C.text_disabled}; background: {C.surface}; }}
    QPushButton[variant="primary"] {{ background: {C.accent}; color: {C.accent_ink}; font-weight: 600; }}
    QPushButton[variant="primary"]:hover {{ background: {C.accent_hover}; }}
    QPushButton[variant="primary"]:pressed {{ background: {C.accent_pressed}; }}
    QPushButton[variant="primary"]:focus {{ border: 1px solid {C.text}; }}
    QPushButton[variant="primary"]:disabled {{ background: {rgba(C.accent, 0.25)}; color: {rgba(C.accent_ink, 0.55)}; }}
    QPushButton[variant="ghost"] {{ background: transparent; color: {C.text_secondary}; }}
    QPushButton[variant="ghost"]:hover {{ background: {C.surface_hover}; color: {C.text}; }}
    QPushButton[variant="ghost"]:disabled {{ color: {C.text_disabled}; background: transparent; }}
    QPushButton[variant="subtle"] {{ background: {rgba(C.border, 0.10)}; color: {C.text}; }}
    QPushButton[variant="subtle"]:hover {{ background: {rgba(C.border, 0.18)}; }}
    QPushButton[variant="danger"] {{ background: {rgba(C.danger, 0.14)}; color: {C.danger}; font-weight: 600; }}
    QPushButton[variant="danger"]:hover {{ background: {rgba(C.danger, 0.24)}; }}
    QPushButton[variant="danger"]:focus {{ border: 1px solid {C.danger}; }}
    QPushButton[size="sm"] {{ min-height: 28px; padding: 0 {S.md}px; font-size: {T.meta}px; border-radius: {R.sm}px; }}

    /* ---------- inputs ---------- */
    QLineEdit, QComboBox, QSpinBox {{
        background: {C.surface_sunken}; color: {C.text}; border: 1px solid {border};
        border-radius: {R.md}px; padding: 0 {S.md}px; min-height: {Size.control}px;
        selection-background-color: {rgba(C.accent, 0.35)}; selection-color: {C.text};
    }}
    QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{ border: 1px solid {border_strong}; }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {C.accent}; }}
    QLineEdit:disabled {{ color: {C.text_disabled}; }}
    QComboBox::drop-down {{ border: none; width: 28px; }}
    QComboBox QAbstractItemView {{
        background: {C.overlay}; border: 1px solid {border_strong}; border-radius: {R.md}px;
        selection-background-color: {C.surface_hover}; selection-color: {C.text}; padding: {S.xs}px; outline: 0;
    }}
    QCheckBox {{ spacing: {S.sm}px; color: {C.text_secondary}; min-height: {Size.control}px; }}
    QCheckBox:hover {{ color: {C.text}; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px; border-radius: 4px; border: 1px solid {border_strong}; background: {C.surface_sunken};
    }}
    QCheckBox::indicator:checked {{ background: {C.accent}; border: 1px solid {C.accent}; image: url("{CHECK_ICON}"); }}
    QCheckBox::indicator:focus {{ border: 1px solid {C.accent}; }}

    /* ---------- plain lists (projects / accounts / sessions) ---------- */
    QListWidget {{ background: transparent; border: none; padding: {S.xs}px; }}
    QListWidget::item {{
        padding: {S.md}px {S.md}px; margin: 2px 0; border-radius: {R.md}px; color: {C.text};
    }}
    QListWidget::item:hover {{ background: {rgba(C.border, 0.08)}; }}
    QListWidget::item:selected {{ background: {accent_subtle}; color: {C.text}; }}
    QListWidget::item:selected:!active {{ background: {rgba(C.border, 0.12)}; }}
    QListWidget[cards="true"] {{ padding: 0; }}
    QListWidget[cards="true"]::item {{ background: transparent; padding: 0; margin: 0; border: none; }}
    QListWidget[cards="true"]::item:hover, QListWidget[cards="true"]::item:selected {{ background: transparent; }}

    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {rgba(C.border, 0.25)}; border-radius: 3px; min-height: 32px; }}
    QScrollBar::handle:vertical:hover {{ background: {rgba(C.border, 0.45)}; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; height: 0; width: 0; }}
    QScrollBar:horizontal {{ height: 0; }}

    /* ---------- menus ---------- */
    QMenu {{
        background: {C.overlay}; border: 1px solid {border_strong}; border-radius: {R.lg}px; padding: {S.xs + 2}px;
    }}
    QMenu::item {{ padding: 8px 14px 8px 12px; border-radius: {R.sm}px; color: {C.text}; min-width: 168px; }}
    QMenu::item:selected {{ background: {C.surface_hover}; }}
    QMenu::item:disabled {{ color: {C.text_disabled}; }}
    QMenu::separator {{ height: 1px; background: {border}; margin: {S.xs + 2}px {S.sm}px; }}

    /* ---------- message boxes / file dialogs inherit the dark surface ---------- */
    QMessageBox {{ background: {C.bg}; }}
    QMessageBox QLabel {{ color: {C.text}; }}
    QInputDialog, QFileDialog {{ background: {C.bg}; }}
    QDialogButtonBox QPushButton {{ min-width: 76px; }}
    """


def apply_dark_titlebar(widget, dark: bool | None = None) -> None:
    """Ask Windows for a native title bar matching the theme, dark or light (best effort)."""
    try:
        import ctypes
        import sys

        from PySide6.QtGui import QGuiApplication

        if sys.platform != "win32" or QGuiApplication.platformName() != "windows":
            return
        hwnd = int(widget.winId())
        value = ctypes.c_int(1 if (C.dark if dark is None else dark) else 0)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 20H1+ / older builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass
