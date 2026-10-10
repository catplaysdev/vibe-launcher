"""Vibe Launcher: explicit accounts, explicit environments."""

import re as _re
from pathlib import Path as _Path


def _detect_version() -> str:
    """The single source is pyproject.toml; installed metadata first, then the checkout's pyproject."""
    try:
        from importlib.metadata import distributions
        # An update that leaves an older vibe_launcher-*.dist-info behind must not make the app report the old version.
        found = [d.version for d in distributions(name="vibe-launcher") if d.version]
        if found:
            return max(found, key=lambda v: tuple(int(n) if n.isdigit() else 0 for n in _re.split(r"[.+-]", v)[:3]))
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
