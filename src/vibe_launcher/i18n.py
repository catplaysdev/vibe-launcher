"""UI catalogs: English first, with explicit locale and English fallback."""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from . import runtime

LOCALES = Path(__file__).with_name("locales")
LANGUAGES = {
    "en": "English",
    "zh-TW": "繁體中文（台灣）",
    "zh-CN": "简体中文（中国大陆）",
    "ja": "日本語",
    "ko": "한국어",
    "de": "Deutsch",
    "es": "Español",
}


def _initial_language() -> str:
    value = os.environ.get("VIBE_LAUNCHER_LANGUAGE")
    if value in LANGUAGES:
        return value
    root = Path(os.environ.get("VIBE_LAUNCHER_HOME") or
                str(Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "VibeLauncher"))
    try:
        value = json.loads(runtime.read_private(root / "ui-state.json")).get("language")
    except (OSError, ValueError, AttributeError):
        value = None
    return value if value in LANGUAGES else "en"


_language = _initial_language()


def language() -> str:
    return _language


def set_language(value: str) -> None:
    global _language
    if value not in LANGUAGES:
        raise ValueError("Unsupported language")
    _language = value


@lru_cache(maxsize=16)
def catalog(locale: str) -> dict[str, str]:
    try:
        data = json.loads((LOCALES / (locale + ".json")).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in data.items()):
            raise ValueError("Invalid translation catalog")
        return data
    except (OSError, ValueError):
        logging.getLogger(__name__).warning("Cannot load translation catalog: %s", locale)
        return {}


def tr(key: str, **values: object) -> str:
    text = catalog(_language).get(key, catalog("en").get(key, catalog("zh-TW").get(key, key)))
    return text.format(**values) if values else text


def environment_name(record: dict) -> str:
    # Local is immutable in the store; translate its label only when displaying it.
    return tr("ui.019e29ea514a") if record.get("id") == "local" and record.get("kind") == "local" else str(record.get("name") or "")
