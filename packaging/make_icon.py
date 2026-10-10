"""Render the launcher logo on its cream plate (as in the app) into ``packaging/vibe-launcher.ico`` (PNG entries, 16-256 px)."""
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QGuiApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from vibe_launcher import icons  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)


def png_bytes(size: int) -> bytes:
    pixmap = icons.logo_pixmap(size, icons.LOGO_LINE, icons.LOGO_EYES, plate=icons.LOGO_PLATE)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    return bytes(data)


def build_ico() -> bytes:
    entries = [(size, png_bytes(size)) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    table, body = b"", b""
    for size, png in entries:
        table += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset + len(body))
        body += png
    return header + table + body


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication([])
    (ROOT / "packaging" / "vibe-launcher.ico").write_bytes(build_ico())
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
