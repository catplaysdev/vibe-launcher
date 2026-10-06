"""One shortcut registry for Qt editors, windows and xterm."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

DEFAULTS = {
    "palette": "Ctrl+K", "sidebar": "Ctrl+B", "search": "Ctrl+F", "create": "Ctrl+N",
    "settings": "Ctrl+,", "navigate": "Ctrl+Shift+Space",
    "window.next": "Ctrl+Alt+PgDown", "window.prev": "Ctrl+Alt+PgUp",
    "next": "Ctrl+Tab", "prev": "Ctrl+Shift+Tab", "workspace.palette": "Ctrl+Shift+K",
    "detach": "Ctrl+Shift+D", "close": "Ctrl+Shift+W", "new": "Ctrl+Shift+T",
    "composer": "Ctrl+Shift+E", "composer.position": "Ctrl+Shift+P",
    "zoom.in": "Ctrl+=", "zoom.out": "Ctrl+-", "zoom.reset": "Ctrl+0",
    **{f"page:{i}": f"Ctrl+{i}" for i in range(1, 6)},
    **{f"goto:{i}": f"Alt+{i}" for i in range(1, 10)},
    "nav.up": "W", "nav.down": "S", "nav.left": "A", "nav.right": "D",
    "nav.input": "Tab",
}
LABELS = {
    "palette": "Command palette", "sidebar": "Toggle sidebar", "search": "Search", "create": "New item",
    "settings": "Settings", "navigate": "Navigation mode", "window.next": "Next window", "window.prev": "Previous window",
    "next": "Next terminal", "prev": "Previous terminal", "workspace.palette": "Workspace command palette",
    "detach": "Detach terminal", "close": "Close terminal", "new": "Open another terminal", "composer": "Focus input / terminal",
    "composer.position": "Move input above / below terminal", "zoom.in": "Terminal text larger",
    "zoom.out": "Terminal text smaller", "zoom.reset": "Terminal text default size", "nav.up": "Navigate up", "nav.down": "Navigate down",
    "nav.left": "Navigate left", "nav.right": "Navigate right", "nav.input": "Switch input area",
    **{f"page:{i}": f"Go to page {i}" for i in range(1, 6)},
    **{f"goto:{i}": f"Go to terminal {i}" for i in range(1, 10)},
}
_bindings = dict(DEFAULTS)
TERMINAL_ACTIONS = {"next", "prev", "workspace.palette", "detach", "close", "new", "composer", "composer.position",
                    "zoom.in", "zoom.out", "zoom.reset", "navigate", "settings", "window.next", "window.prev", *[f"goto:{i}" for i in range(1, 10)]}


def validate(overrides: object) -> dict[str, str]:
    if not isinstance(overrides, dict) or overrides.keys() - DEFAULTS.keys():
        raise ValueError("Unknown shortcut action")
    bindings = {**DEFAULTS, **overrides}
    seen: dict[tuple[bool, int], str] = {}
    for action, text in bindings.items():
        if not isinstance(text, str):
            raise ValueError("Shortcut must be text: " + action)
        sequence = QKeySequence.fromString(text, QKeySequence.SequenceFormat.PortableText)
        if sequence.count() != 1 or sequence[0].key() == Qt.Key.Key_unknown:
            raise ValueError("Use one valid key combination: " + action)
        navigation = action.startswith("nav.")
        if not navigation and not sequence[0].keyboardModifiers() and not Qt.Key.Key_F1 <= sequence[0].key() <= Qt.Key.Key_F35:
            raise ValueError("Shortcuts outside navigation mode need Ctrl, Alt or a function key: " + action)
        code = sequence[0].toCombined()
        identity = (navigation, code)
        if identity in seen:
            raise ValueError("Shortcut conflict: " + seen[identity] + " / " + action)
        if navigation and sequence[0].key() in {Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            raise ValueError("Enter and Escape are reserved for confirming and leaving navigation")
        seen[identity] = action
        bindings[action] = sequence.toString(QKeySequence.SequenceFormat.PortableText)
    return bindings


def configure(overrides: dict) -> None:
    global _bindings
    _bindings = validate(overrides)


def bindings() -> dict[str, str]:
    return dict(_bindings)


def action_for(event, *, navigation: bool = False) -> str | None:
    key = event.key()
    # The number pad's keys count as the main keyboard's; "+" (Shift+= or the number pad) counts as "=", so the
    # zoom shortcut Ctrl+= also answers to Ctrl and "+".
    modifiers = event.modifiers().value & ~Qt.KeyboardModifier.KeypadModifier.value   # (int: ~ on the flag gives 0)
    if key == Qt.Key.Key_Backtab:
        key = Qt.Key.Key_Tab
    elif key == Qt.Key.Key_Plus:
        key, modifiers = Qt.Key.Key_Equal, modifiers & ~Qt.KeyboardModifier.ShiftModifier.value
    sequence = QKeySequence(modifiers | int(key))
    for action, text in _bindings.items():
        if action.startswith("nav.") == navigation and sequence == QKeySequence(text):
            return action
    return None


def web_bindings() -> list[dict]:
    names = {"Space": " ", "Esc": "Escape", "PgUp": "PageUp", "PgDown": "PageDown", "Del": "Delete",
             "Ins": "Insert", "Backspace": "Backspace", "Return": "Enter"}
    result = []
    for action, text in _bindings.items():
        if action not in TERMINAL_ACTIONS:
            continue
        sequence = QKeySequence(text)
        combination = sequence[0]
        modifiers = combination.keyboardModifiers()
        key_text = QKeySequence(int(combination.key())).toString(QKeySequence.SequenceFormat.PortableText)
        result.append({"action": action, "key": names.get(key_text, key_text).lower(),
                       "ctrl": bool(modifiers & Qt.KeyboardModifier.ControlModifier),
                       "shift": bool(modifiers & Qt.KeyboardModifier.ShiftModifier),
                       "alt": bool(modifiers & Qt.KeyboardModifier.AltModifier),
                       "meta": bool(modifiers & Qt.KeyboardModifier.MetaModifier)})
    return result
