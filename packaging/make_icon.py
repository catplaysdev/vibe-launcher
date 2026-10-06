"""Render ``assets/logo.svg`` into ``packaging/vibe-launcher.ico`` (PNG-compressed entries, 16-256 px)."""
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parents[1]
SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication([])
    renderer = QSvgRenderer(str(ROOT / "src" / "vibe_launcher" / "assets" / "logo.svg"))
    if not renderer.isValid():
        print("logo.svg could not be read", file=sys.stderr)
        return 1
    entries = []
    for size in SIZES:
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        entries.append((size, bytes(data)))
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    table, body = b"", b""
    for size, png in entries:
        table += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset + len(body))
        body += png
    (ROOT / "packaging" / "vibe-launcher.ico").write_bytes(header + table + body)
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
