"""How the launcher starts itself: the one place that knows about running from source versus a packaged build.

From source, helper jobs run as ``python -I -m vibe_launcher.<module> ...``. In a PyInstaller build there is no ``python``:
``sys.executable`` is the program itself, so helpers go through ``vibe-launcher-cli.exe <module> ...`` (a console build
that shares the files of the windowed ``Vibe Launcher.exe``; see ``entry.py`` and ``packaging/``). Never build such a
command line anywhere else, and never read package files through ``__file__`` of a module that is not listed in
``packaging/vibe-launcher.spec``.
"""
from __future__ import annotations

import sys
from pathlib import Path

GUI_NAME = "Vibe Launcher.exe"
CLI_NAME = "vibe-launcher-cli.exe"
# Helper programs ``entry.py`` can run; each is ``vibe_launcher.<name>`` with a ``main()`` that reads ``sys.argv``.
HELPERS = ("runner", "updates", "askpass")


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """The folder holding the packaged program's files (``_internal``); the package folder when running from source."""
    if frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def install_dir() -> Path:
    """The folder with the program's executables (the install folder of a packaged build)."""
    return Path(sys.executable).resolve().parent


def _console_python() -> Path:
    python = Path(sys.executable)
    return python.with_name("python.exe") if python.name.lower() == "pythonw.exe" else python


def helper_program() -> str:
    """The executable that runs helper modules."""
    return str(install_dir() / CLI_NAME) if frozen() else str(_console_python())


def command(module: str, *args: str) -> list[str]:
    """The argument list that runs helper ``module`` (one of ``HELPERS``) with ``args``."""
    if module not in HELPERS:
        raise ValueError("Unknown helper")
    if frozen():
        return [helper_program(), module, *args]
    return [helper_program(), "-I", "-m", "vibe_launcher." + module, *args]


def gui_launch() -> tuple[str, list[str]]:
    """``(program, arguments)`` that start a fresh copy of the windowed launcher."""
    if frozen():
        gui = install_dir() / GUI_NAME
        return str(gui if gui.exists() else Path(sys.executable)), []
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    return str(windowless if windowless.exists() else python), ["-m", "vibe_launcher.ui"]


def helper_environment() -> tuple[str, str]:
    """``(VIBE_SSH_PYTHON, VIBE_SSH_ARGS)`` for the native askpass bridge, which starts the password dialog helper."""
    if frozen():
        return helper_program(), "askpass"
    return helper_program(), "-I -m vibe_launcher.askpass"


def runtime_source() -> bytes:
    """``runtime.py`` as text to ship to SSH hosts (a packaged build carries the file as data, not as bytecode only)."""
    if frozen():
        return (bundle_dir() / "vibe_launcher" / "runtime.py").read_bytes()
    return (Path(__file__).resolve().parent / "runtime.py").read_bytes()
