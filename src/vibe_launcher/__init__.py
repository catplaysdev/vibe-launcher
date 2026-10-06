"""Vibe Launcher: explicit accounts, explicit environments."""

import re as _re
from pathlib import Path as _Path


def _detect_version() -> str:
    """The single source is pyproject.toml; installed metadata first, then the checkout's pyproject."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("vibe-launcher")
        except PackageNotFoundError:
            pass
    except ImportError:  # pragma: no cover - stdlib on supported Pythons
        pass
    try:
        text = (_Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
        match = _re.search(r'(?m)^version\s*=\s*"(\d+\.\d+\.\d+[^"]*)"', text)
        if match:
            return match.group(1)
    except (OSError, IndexError):
        pass
    return "0.0.0"


__version__ = _detect_version()
