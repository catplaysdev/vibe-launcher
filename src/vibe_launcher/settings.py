"""Nonsecret shell and CLI extension manifests, shared by UI and service.

A settings.json that cannot be used is left untouched while the launcher falls back to defaults.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from . import runtime

BUILTIN_PROFILES = [
    {"id": "auto", "label": "Default shell", "platforms": ["windows", "linux"], "command": []},
    {"id": "cmd", "label": "Command Prompt", "platforms": ["windows"], "command": ["cmd", "/K"]},
    {"id": "powershell", "label": "Windows PowerShell", "platforms": ["windows"], "command": ["powershell", "-NoLogo"]},
    {"id": "pwsh", "label": "PowerShell 7", "platforms": ["windows", "linux"], "command": ["pwsh", "-NoLogo"]},
    {"id": "bash", "label": "Bash", "platforms": ["linux"], "command": ["bash", "-l"]},
]
SETTINGS_VERSION = 1
DEFAULT_SETTINGS = {"version": SETTINGS_VERSION, "shells": {"windows": "auto", "linux": "bash"}, "plugins": []}
log = logging.getLogger(__name__)


def validate_settings(data: object) -> dict:
    """Validate the settings; fields this build does not know are dropped."""
    if not isinstance(data, dict) or data.get("version") != SETTINGS_VERSION or type(data.get("version")) is not int:
        raise ValueError(f"Settings need version {SETTINGS_VERSION}")
    plugins = data.get("plugins", [])
    if not isinstance(plugins, list) or len(plugins) > 32:
        raise ValueError("At most 32 CLI plugins are supported")
    seen = {p["id"] for p in BUILTIN_PROFILES}
    for plugin in plugins:
        runtime.validate_terminal_profile(plugin)
        if not plugin.get("profile_env"):
            raise ValueError("CLI plugins need their dedicated profile_env variable")
        if plugin["id"] in seen:
            raise ValueError("Duplicate terminal profile id: " + plugin["id"])
        seen.add(plugin["id"])
    shells = data.get("shells", {})
    if not isinstance(shells, dict) or shells.keys() - {"windows", "linux"}:
        raise ValueError("Shell defaults must contain only windows and linux")
    for platform in ("windows", "linux"):
        selected = shells.get(platform, DEFAULT_SETTINGS["shells"][platform])
        if not isinstance(selected, str) or not any(p["id"] == selected and platform in p["platforms"] for p in BUILTIN_PROFILES + plugins):
            raise ValueError("Unknown or incompatible default shell for " + platform)
    result = {"version": SETTINGS_VERSION, "shells": {p: shells.get(p, DEFAULT_SETTINGS["shells"][p]) for p in ("windows", "linux")},
              "plugins": plugins}
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 65536:
        raise ValueError("CLI settings exceed the 64 KiB size limit")
    return result


def _read_raw(path: Path) -> dict | None:
    """The stored object, or None when the file is missing; raises on unreadable / non-object content."""
    try:
        data = json.loads(runtime.read_private(path, limit=65536))
    except FileNotFoundError:
        return None
    if not isinstance(data, dict):
        raise ValueError("settings.json must contain a JSON object")
    return data


def load_settings(root: Path) -> dict:
    """Never raises for a bad file: log, use defaults, and leave the file as it is."""
    path = root / "settings.json"
    try:
        data = _read_raw(path)
        return validate_settings(DEFAULT_SETTINGS if data is None else data)
    except (OSError, ValueError, TypeError) as error:
        log.warning("Ignoring unusable %s (%s); using defaults", path.name, error)
        return validate_settings(DEFAULT_SETTINGS)


def save_settings(root: Path, data: dict) -> None:
    """Save validated settings; an existing file that cannot be read is set aside, never silently replaced."""
    path = root / "settings.json"
    result = validate_settings(data)
    try:
        _read_raw(path)
    except (OSError, ValueError, TypeError):
        if path.exists():  # unusable but present: keep a copy instead of silently replacing it
            runtime.assert_private_path(path)
            aside = path.with_name(f"settings.unreadable-{time.strftime('%Y%m%d-%H%M%S')}.json")
            if not aside.exists():
                path.replace(aside)
    runtime.write_json(path, result)


def profiles(root: Path, platform: str) -> list[dict]:
    return [p for p in BUILTIN_PROFILES + load_settings(root)["plugins"] if platform in p["platforms"]]


def selected_profile(root: Path, environment: dict, selected: str = "") -> dict | None:
    platform = "windows" if environment["kind"] == "local" and os.name == "nt" else "linux"
    data = load_settings(root)
    selected = selected or data["shells"][platform]
    profile = next((p for p in profiles(root, platform) if p["id"] == selected), None)
    if profile is None:
        raise ValueError("Terminal profile is unavailable in this environment")
    return dict(profile) if profile["command"] else None
