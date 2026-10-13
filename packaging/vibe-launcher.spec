# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe: one folder holding the windowed ``Vibe Launcher.exe`` and the console ``vibe-launcher-cli.exe``.

    uv run --group build pyinstaller --noconfirm --distpath dist-app --workpath build-app packaging/vibe-launcher.spec

Why it looks like this (keep it when editing; see AGENTS.md "Packaging and updates"):
* onedir, no UPX: starts fast, nothing is unpacked into %TEMP% on every start, fewer antivirus false alarms.
* two executables over one set of files: the window must not open a console; the helpers (job runner, SSH password dialog,
  updater) must have one (their output is read by the terminal / by the OpenSSH bridge). ``selfcmd.py`` picks between them.
* only Qt modules the code imports are collected (no collect-all); Chromium language packs and Qt translations are cut down
  to the languages the launcher ships.
* ``runtime.py`` is also shipped as a plain file: it is sent verbatim to SSH hosts (``selfcmd.runtime_source``).
"""
import re
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src" / "vibe_launcher"
KEEP_LOCALES = {"en-US", "zh-TW", "zh-CN", "ja", "ko", "de", "es", "es-419"}      # Chromium language packs
ICON = ROOT / "packaging" / "vibe-launcher.ico"

datas = [
    (str(SRC / "assets"), "vibe_launcher/assets"),
    (str(SRC / "locales"), "vibe_launcher/locales"),
    (str(SRC / "runtime.py"), "vibe_launcher"),
    (str(SRC / "askpass_bridge.cs"), "vibe_launcher"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
] + copy_metadata("vibe-launcher")            # the version comes from the installed metadata

analysis = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    # Helpers are imported by name in entry.py, which static analysis cannot follow.
    hiddenimports=["vibe_launcher.runner", "vibe_launcher.updates", "vibe_launcher.askpass", "vibe_launcher.claude_hook"],
    excludes=["tkinter", "unittest", "pytest", "test", "distutils", "setuptools", "pip",
              "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtMultimedia",
              "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtBluetooth", "PySide6.QtSensors", "PySide6.QtPdf"],
    noarchive=False,
)


def wanted(name: str) -> bool:
    """Drop files the launcher never reads: brand logos (never shipped), other-language packs, Qt's own translations."""
    path = name.replace("\\", "/")
    if "vibe_launcher/assets/brand/" in path:
        return False
    pack = re.search(r"qtwebengine_locales/([^/]+)\.pak$", path)
    if pack:
        return pack.group(1) in KEEP_LOCALES
    if re.search(r"PySide6/translations/", path) and path.endswith(".qm"):
        return False
    if re.search(r"\.debug\.(pak|bin)$|qtwebengine_devtools_resources", path):
        return False                      # Chromium developer tools and debug twins of the resource packs
    if path.startswith("PySide6/qml/"):
        return False                      # QML modules: the launcher only creates an empty Quick widget, never loads QML
    if re.search(r"(^|/)Qt6(3D\w*|Quick3D\w*|Graphs\w*|Charts\w*|Location\w*|Pdf\w*|Multimedia\w*|DataVisualization\w*|Sql|Bluetooth|Sensors|Nfc|SerialPort|TextToSpeech)\.dll$", path):
        return False                      # modules the launcher does not import
    if re.search(r"(^|/)icu(uc|in|dt)\d*\.dll$", path, re.IGNORECASE):
        # Qt on Windows uses the system ICU (System32\icuuc.dll, plain C exports). PyInstaller can pick up another
        # program's ICU from PATH instead (LibreOffice ships a versioned ICU 78): QtCore then fails to load (error 127).
        return False
    return True


analysis.datas = [item for item in analysis.datas if wanted(item[0])]
analysis.binaries = [item for item in analysis.binaries if wanted(item[0])]
pyz = PYZ(analysis.pure)

common = dict(exclude_binaries=True, upx=False, strip=False, icon=str(ICON) if ICON.exists() else None)
window = EXE(pyz, analysis.scripts, [], name="Vibe Launcher", console=False, **common)
helper = EXE(pyz, analysis.scripts, [], name="vibe-launcher-cli", console=True, **common)
COLLECT(window, helper, analysis.binaries, analysis.datas, strip=False, upx=False, name="Vibe Launcher")
