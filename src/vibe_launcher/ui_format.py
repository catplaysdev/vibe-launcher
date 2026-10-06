"""Small display helpers shared by the window and its mixins."""

from __future__ import annotations

from .i18n import environment_name, tr

from datetime import datetime
from typing import Any

TONE_TOKENS = {"ok": "success", "info": "info", "warn": "warning", "danger": "danger", "unknown": "neutral"}   # status tone -> colour token


def _value(record: dict[str, Any], key: str, fallback: str = "") -> str:
    if key == "name" and record.get("id") == "local" and record.get("kind") == "local":
        return environment_name(record)
    value = record.get(key, fallback)
    return str(value) if value is not None else fallback


def _display_time(raw: str) -> str:
    if not raw:
        return tr('ui_format.9bd11dda28fc')
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return raw
