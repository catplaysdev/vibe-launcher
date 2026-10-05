"""Tiny non-secret UI preferences (sidebar state, pinned environments).

Kept out of the SQLite store on purpose: these are view settings, not launcher
records, so they are never part of exported/imported configuration.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from .theme import TERMINAL_FONT_SIZES, THEMES, valid_color


class UiPrefs:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.sidebar_collapsed = False
        self.pinned: set[str] = set()
        self.external_terminal = False        # open terminals in separate console windows instead of the workspace
        self.workspace_layout = "tabs"
        self.project_tags: list[str] = []      # tag filter last used on the Projects page
        self.workspace_tags: list[str] = []    # ...and in the workspace's quick-open list
        # Appearance
        self.theme = "deep-sea"                 # an id from theme.THEMES
        self.accent = ""                        # custom accent "#rrggbb"; "" = the theme's own
        self.terminal_font_size = 14
        self.cursor_blink = False
        self.composer_default = True            # Codex / Claude terminals open with the input bar
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
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
        self.theme = data.get("theme") if data.get("theme") in THEMES else "deep-sea"
        self.accent = valid_color(data.get("accent"))
        size = data.get("terminal_font_size")
        self.terminal_font_size = size if isinstance(size, int) and not isinstance(size, bool) and size in TERMINAL_FONT_SIZES else 14
        self.cursor_blink = data.get("cursor_blink") is True
        self.composer_default = data.get("composer_default", True) is not False

    def save(self) -> None:
        payload = {"sidebar_collapsed": self.sidebar_collapsed, "pinned_environments": sorted(self.pinned),
                   "external_terminal": self.external_terminal, "workspace_layout": self.workspace_layout,
                   "project_tags": self.project_tags, "workspace_tags": self.workspace_tags,
                   "theme": self.theme, "accent": self.accent, "terminal_font_size": self.terminal_font_size,
                   "cursor_blink": self.cursor_blink, "composer_default": self.composer_default}
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            pass  # View preferences are best-effort; never block the UI on them.
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
