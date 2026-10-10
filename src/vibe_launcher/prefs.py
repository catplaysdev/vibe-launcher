"""Tiny non-secret UI preferences (sidebar state, pinned environments).

Kept out of the SQLite store on purpose: these are view settings, not launcher
records, so they are never part of exported/imported configuration.
A value this build cannot use falls back to its default; keys it does not know are dropped on the next save.
"""

from __future__ import annotations

import json
from pathlib import Path

from .theme import TERMINAL_FONT_SIZES, THEMES, UI_SCALES, valid_color, valid_font_family
from . import runtime



def valid_anchor(value: object) -> list[float] | None:
    """A toast anchor is None (default corner) or [x_ratio, y_ratio] with both ratios in 0..1."""
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1 for v in value)):
        return [float(value[0]), float(value[1])]
    return None


def valid_workspace_name(value: object) -> str:
    """A display name: one line, at most 64 characters; anything else falls back to "" (the translated default)."""
    if not isinstance(value, str):
        return ""
    name = " ".join(value.split())
    return name if len(name) <= 64 and name.isprintable() else ""


def valid_desktop(value: object) -> dict:
    """The default desktop's saved arrangement: the same slot shape as a saved workspace record (store.py)."""
    result = {"layout": "tabs", "slots": []}
    if not isinstance(value, dict):
        return result
    result["layout"] = "grid" if value.get("layout") == "grid" else "tabs"
    slots = value.get("slots")
    for slot in (slots if isinstance(slots, list) else [])[:16]:
        if not isinstance(slot, dict) or slot.get("kind") not in {"agent", "shell"}:
            continue
        clean = {key: slot[key] for key in ("preset_id", "kind", "account_id", "environment_id", "terminal_profile")
                 if isinstance(slot.get(key), str) and 0 < len(slot[key]) <= 128}
        if "preset_id" in clean:
            result["slots"].append(clean)
    return result


def valid_ui_scale(value: object) -> int:
    return value if type(value) is int and value in UI_SCALES else 100


def stored_ui_scale(path: Path) -> int:
    """The interface scale saved in ``ui-state.json``; read before the application exists (Qt needs it then)."""
    try:
        data = json.loads(runtime.read_private(Path(path)))
    except (OSError, ValueError):
        return 100
    return valid_ui_scale(data.get("ui_scale")) if isinstance(data, dict) else 100


class UiPrefs:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.sidebar_collapsed = False
        self.pinned: set[str] = set()
        self.project_icons: dict[str, dict] = {}   # project id -> icon entry (see project_icons.valid_entry)
        self.workspace_icons: dict[str, dict] = {}  # workspace id ("@default" for the unsaved default desktop) -> entry
        self.default_workspace_name = ""        # the default desktop's own name; "" = the translated "Default workspace"
        self.default_workspace: dict = {"layout": "tabs", "slots": []}   # the default desktop's terminals, kept as you go
        self.last_workspace = ""                # the desktop shown when the launcher last ran ("" = default)
        self.confirm_reload = True              # ask before reloading a terminal
        self.confirm_send = True                # ask before sending the input bar's text
        self.external_terminal = False        # open terminals in separate console windows instead of the workspace
        self.workspace_layout = "tabs"
        self.project_tags: list[str] = []      # tag filter last used on the Projects page
        self.workspace_tags: list[str] = []    # ...and in the workspace's quick-open list
        # Appearance
        self.theme = "deep-sea"                 # an id from theme.THEMES
        self.accent = ""                        # custom accent "#rrggbb"; "" = the theme's own
        self.terminal_font_size = 14
        self.ui_font = ""                       # interface font family; "" = built-in
        self.terminal_font = ""                 # terminal font family; "" = built-in
        self.ui_scale = 100                     # whole-window zoom in percent; takes effect after a restart
        self.cursor_blink = False
        self.composer_default = True            # Codex / Claude terminals open with the input bar
        from .i18n import language
        self.language = language()
        self.shortcuts: dict[str, str] = {}
        self.composer_position = "bottom"
        self.toast_anchor: list[float] | None = None   # [x_ratio, y_ratio] in the window; None = default corner
        self.update_check = True                # look for newer launcher versions in the background
        self.minimize_to_tray = False           # closing the window hides it to the system tray instead of quitting
        self.sessions_grouping = "workspace"    # Sessions page: workspace > project > terminal, or one list by "time"
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(runtime.read_private(self.path))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self.sidebar_collapsed = bool(data.get("sidebar_collapsed", False))
        self.external_terminal = bool(data.get("external_terminal", False))
        self.workspace_layout = data.get("workspace_layout") if data.get("workspace_layout") in {"tabs", "grid"} else "tabs"
        for name in ("project_tags", "workspace_tags"):
            value = data.get(name, [])
            setattr(self, name, [t for t in value if isinstance(t, str) and t][:12] if isinstance(value, list) else [])
        pinned = data.get("pinned_environments", [])
        self.pinned = {p for p in pinned if isinstance(p, str)} if isinstance(pinned, list) else set()
        from .project_icons import valid_entries
        self.project_icons = valid_entries(data.get("project_icons", {}))
        self.workspace_icons = valid_entries(data.get("workspace_icons", {}))
        self.default_workspace_name = valid_workspace_name(data.get("default_workspace_name"))
        self.default_workspace = valid_desktop(data.get("default_workspace"))
        last = data.get("last_workspace")
        self.last_workspace = last if isinstance(last, str) and len(last) <= 128 and "@" not in last else ""
        self.confirm_reload = data.get("confirm_reload", True) is not False
        self.confirm_send = data.get("confirm_send", True) is not False
        self.theme = data.get("theme") if data.get("theme") in THEMES else "deep-sea"
        self.accent = valid_color(data.get("accent"))
        size = data.get("terminal_font_size")
        self.terminal_font_size = size if isinstance(size, int) and not isinstance(size, bool) and size in TERMINAL_FONT_SIZES else 14
        self.ui_font = valid_font_family(data.get("ui_font"))
        self.terminal_font = valid_font_family(data.get("terminal_font"))
        self.ui_scale = valid_ui_scale(data.get("ui_scale"))
        self.cursor_blink = data.get("cursor_blink") is True
        self.composer_default = data.get("composer_default", True) is not False
        from .i18n import LANGUAGES
        from .shortcuts import validate
        self.language = data.get("language") if data.get("language") in LANGUAGES else self.language
        self.composer_position = "top" if data.get("composer_position") == "top" else "bottom"
        try:
            validate(data.get("shortcuts", {}))
            self.shortcuts = dict(data.get("shortcuts", {}))
        except (ValueError, TypeError):
            self.shortcuts = {}
        self.toast_anchor = valid_anchor(data.get("toast_anchor"))
        self.update_check = data.get("update_check", True) is not False
        self.minimize_to_tray = data.get("minimize_to_tray", False) is True
        self.sessions_grouping = "time" if data.get("sessions_grouping") == "time" else "workspace"

    def snapshot(self) -> dict:
        """The known preferences in their stored (JSON) form."""
        return {"sidebar_collapsed": self.sidebar_collapsed, "pinned_environments": sorted(self.pinned),
                "external_terminal": self.external_terminal, "workspace_layout": self.workspace_layout,
                "project_tags": list(self.project_tags), "workspace_tags": list(self.workspace_tags),
                "project_icons": {k: dict(v) for k, v in sorted(self.project_icons.items())},
                "workspace_icons": {k: dict(v) for k, v in sorted(self.workspace_icons.items())},
                "default_workspace_name": valid_workspace_name(self.default_workspace_name),
                "default_workspace": valid_desktop(self.default_workspace), "last_workspace": self.last_workspace,
                "confirm_reload": bool(self.confirm_reload), "confirm_send": bool(self.confirm_send),
                "theme": self.theme, "accent": self.accent, "terminal_font_size": self.terminal_font_size,
                "ui_font": valid_font_family(self.ui_font), "terminal_font": valid_font_family(self.terminal_font),
                "ui_scale": valid_ui_scale(self.ui_scale), "cursor_blink": self.cursor_blink, "composer_default": self.composer_default,
                "language": self.language, "shortcuts": dict(self.shortcuts), "composer_position": self.composer_position,
                "toast_anchor": valid_anchor(self.toast_anchor), "update_check": bool(self.update_check),
                "minimize_to_tray": bool(self.minimize_to_tray),
                "sessions_grouping": self.sessions_grouping}

    def save(self) -> None:
        try:
            runtime.write_json(self.path, self.snapshot())
        except OSError:
            pass  # View preferences are best-effort; never block the UI on them.
