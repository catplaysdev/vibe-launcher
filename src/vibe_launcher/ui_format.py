"""Small display helpers shared by the window and its mixins."""

from __future__ import annotations

from datetime import datetime
from typing import Any

TONE_TOKENS = {"ok": "success", "info": "info", "warn": "warning", "danger": "danger", "unknown": "neutral"}   # status tone -> colour token


def _value(record: dict[str, Any], key: str, fallback: str = "") -> str:
    value = record.get(key, fallback)
    return str(value) if value is not None else fallback


def _display_time(raw: str) -> str:
    if not raw:
        return "尚未使用"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return raw
