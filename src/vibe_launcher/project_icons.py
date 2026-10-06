"""Project icons: a built-in glyph, or the user's own picture / SVG (an SVG or glyph can take any colour).

The choice is a view preference, kept in ``ui-state.json`` (``project_icons``: project id -> entry) like pinned
environments, so it never changes the project records. Pictures are copied into ``<data>/project-icons/`` under a random
name; the original file is not referenced again. SVGs are drawn by Qt SVG, which loads nothing external and runs no script.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtSvg import QSvgRenderer

from . import icons, runtime
from .i18n import tr
from .theme import C, R, TINT_A, TINT_STRONG_A, qcolor, resolve, section_hue, valid_color

GLYPHS = ("folder", "briefcase", "terminal", "server", "monitor", "cpu", "layers", "grid", "github", "home",
          "star", "heart", "sparkles", "puzzle", "shield", "palette", "wrench", "activity", "key", "send",
          "database", "globe", "cloud", "code", "git-branch", "zap", "rocket", "box", "book", "flask", "bug",
          "compass", "building", "crane", "hard-hat", "chart-bar", "clipboard", "map", "camera")
EXTENSIONS = (".svg", ".png", ".jpg", ".jpeg", ".webp")
MAX_BYTES = 1048576
MAX_ENTRIES = 500
FOLDER = "project-icons"
_FILE_NAME = re.compile(r"[0-9a-f]{32}\.(svg|png|jpg|jpeg|webp)")

_root: Path | None = None
_entries: dict[str, dict] = {}
_cache: dict[tuple, QPixmap | None] = {}


# --------------------------------------------------------------------------- entries


def valid_entry(value: object) -> dict | None:
    """``{"kind": "glyph" | "file", "name": ..., "color": "#RRGGBB" | ""}``, or None when unusable."""
    if not isinstance(value, dict):
        return None
    kind, name, color = value.get("kind"), value.get("name"), value.get("color", "")
    if kind == "glyph" and name in GLYPHS or kind == "file" and isinstance(name, str) and _FILE_NAME.fullmatch(name):
        return {"kind": kind, "name": name, "color": valid_color(color) if isinstance(color, str) else ""}
    return None


def valid_entries(value: object) -> dict[str, dict]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, entry in list(value.items())[:MAX_ENTRIES]:
        entry = valid_entry(entry)
        if isinstance(key, str) and 0 < len(key) <= 128 and entry:
            result[key] = entry
    return result


def configure(root: Path, entries: dict[str, dict]) -> None:
    """Where pictures live and which project has which icon (the window calls this after loading / saving prefs)."""
    global _root, _entries
    _root, _entries = Path(root), dict(entries)


def entry_for(project_id: str) -> dict | None:
    return _entries.get(project_id)


def signature(project_id: str) -> tuple:
    """Changes whenever the project's icon does (lets list rows notice it)."""
    entry = _entries.get(project_id) or {}
    return ("@project", project_id, entry.get("kind", ""), entry.get("name", ""), entry.get("color", ""))


# --------------------------------------------------------------------------- pictures on disk


def folder(root: Path) -> Path:
    return Path(root) / FOLDER


def check_picture(data: bytes, extension: str) -> None:
    """Raise ValueError unless ``data`` is a picture this module can draw."""
    extension = extension.lower()
    if extension not in EXTENSIONS:
        raise ValueError(tr("Use an SVG, PNG, JPG or WebP picture"))
    if not data or len(data) > MAX_BYTES:
        raise ValueError(tr("The picture must be smaller than 1 MB"))
    if extension == ".svg":
        if not QSvgRenderer(QByteArray(data)).isValid():
            raise ValueError(tr("This SVG cannot be read"))
    elif QImage.fromData(data).isNull():
        raise ValueError(tr("This picture cannot be read"))


def read_source(path: Path) -> tuple[bytes, str]:
    """The picture the user picked, checked; (bytes, extension)."""
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(tr("The picture must be smaller than 1 MB"))
    data = path.read_bytes()
    check_picture(data, path.suffix)
    return data, path.suffix.lower()


def store_picture(root: Path, data: bytes, extension: str) -> str:
    """Copy a checked picture into the launcher's data folder; returns its stored name."""
    check_picture(data, extension)
    target_dir = folder(root)
    runtime.private_dir(target_dir)
    name = uuid.uuid4().hex + (".jpg" if extension.lower() == ".jpeg" else extension.lower())
    target = target_dir / name
    runtime.assert_private_path(target)
    with open(target, "xb") as stream:
        runtime.protect_path(target)
        stream.write(data)
    return name


def remove_picture(root: Path, name: str) -> None:
    if not _FILE_NAME.fullmatch(name or ""):
        return
    path = folder(root) / name
    try:
        runtime.assert_private_path(path)
        path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def _load(name: str) -> bytes | None:
    if _root is None or not _FILE_NAME.fullmatch(name):
        return None
    try:
        data = runtime.read_private_bytes(folder(_root) / name, MAX_BYTES)
    except (OSError, ValueError):
        return None
    return data if len(data) <= MAX_BYTES else None


# --------------------------------------------------------------------------- drawing


_PAINT = re.compile(r'((?:fill|stroke|stop-color)\s*[=:]\s*["\']?)(?!none\b|transparent\b|url\()([#\w(),.%\s]+?)(?=["\';}\s>/])')


def recolor_svg(text: str, color: str) -> str:
    """Every fill / stroke / gradient stop becomes ``color`` (``none`` stays none); unpainted shapes default to it too."""
    text = _PAINT.sub(lambda m: m.group(1) + color, text)
    return re.sub(r"<svg\b", f'<svg fill="{color}"', text, count=1) if not re.search(r"<svg\b[^>]*\sfill=", text) else text


def render(data: bytes, extension: str, size: int, dpr: float = 2.0, color: str = "") -> QPixmap | None:
    """A ``size`` px square picture (kept in proportion, centred) from picture bytes."""
    pm = QPixmap(round(size * dpr), round(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    try:
        if extension == ".svg":
            text = data.decode("utf-8", "replace")
            renderer = QSvgRenderer(QByteArray((recolor_svg(text, color) if color else text).encode("utf-8")))
            if not renderer.isValid():
                return None
            box = renderer.defaultSize()
            fitted = QSize(box.width() or size, box.height() or size).scaled(QSize(size, size), Qt.AspectRatioMode.KeepAspectRatio)
            renderer.render(painter, QRectF((size - fitted.width()) / 2, (size - fitted.height()) / 2, fitted.width(), fitted.height()))
        else:
            image = QImage.fromData(data)
            if image.isNull():
                return None
            fitted = image.size().scaled(QSize(size, size), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF((size - fitted.width()) / 2, (size - fitted.height()) / 2, fitted.width(), fitted.height())
            clip = QPainterPath()
            clip.addRoundedRect(target, size * 0.18, size * 0.18)       # photos get the same soft corners as tiles
            painter.setClipPath(clip)
            painter.drawImage(target, image)
    finally:
        painter.end()
    return pm


def _picture(entry: dict, size: int, dpr: float) -> QPixmap | None:
    name = entry["name"]
    extension = os.path.splitext(name)[1]
    key = (name, entry.get("color", ""), size, round(dpr, 2))
    if key not in _cache:
        data = _load(name)
        _cache[key] = render(data, extension, size, dpr, entry.get("color", "") if extension == ".svg" else "") if data else None
    return _cache[key]


def draw(painter: QPainter, rect: QRectF, project_id: str = "", *, entry: dict | None = None, selected: bool = False,
         dpr: float = 2.0, radius: float = R.md, preview: tuple[bytes, str] | None = None) -> None:
    """The project's tile in ``rect``: a tinted square with its glyph / SVG, or its picture filling the tile.

    ``entry`` overrides the stored choice and ``preview`` (bytes, extension) draws a picture not stored yet (the dialog).
    """
    entry = entry if entry is not None else _entries.get(project_id)
    hue = resolve(section_hue("projects"))
    color = (entry or {}).get("color") or hue
    side = min(rect.width(), rect.height())
    inner = round(side * 0.53)
    picture = None
    raster = False
    if preview is not None:
        data, extension = preview
        raster = extension != ".svg"
        picture = render(data, extension, round(side) if raster else inner, dpr, "" if raster else (entry or {}).get("color", ""))
    elif entry and entry["kind"] == "file":
        raster = not entry["name"].endswith(".svg")
        picture = _picture(entry, round(side) if raster else inner, dpr)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    if not raster or picture is None:
        painter.setBrush(qcolor(color, TINT_STRONG_A if selected else TINT_A))
        painter.drawRoundedRect(rect, radius, radius)
    if picture is not None:
        size = round(side) if raster else inner
        painter.drawPixmap(round(rect.center().x() - size / 2), round(rect.center().y() - size / 2), picture)
    else:
        name = entry["name"] if entry and entry["kind"] == "glyph" else "folder"
        painter.drawPixmap(round(rect.center().x() - inner / 2), round(rect.center().y() - inner / 2),
                           icons.pixmap(name, QColor(color), inner, dpr))
    painter.restore()
