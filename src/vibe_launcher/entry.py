"""Program entry for packaged builds (``Vibe Launcher.exe`` and ``vibe-launcher-cli.exe``) and ``python -m vibe_launcher.entry``.

No arguments starts the window. ``<helper> ...`` runs one of ``selfcmd.HELPERS`` as the helper's own ``__main__`` would, so
helper jobs behave the same from source and from a packaged build.
"""
from __future__ import annotations

import importlib
import sys

from . import selfcmd


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in selfcmd.HELPERS:
        name, rest = argv[0], argv[1:]
        sys.argv = [name, *rest]                       # the helpers read sys.argv like a script does
        module = importlib.import_module("vibe_launcher." + name)
        return int(module.main() or 0)
    if argv == ["--selftest"]:
        from .selftest import main as selftest
        return selftest()
    if argv:
        sys.stderr.write("Unknown command: " + argv[0] + "\n")
        return 2
    from .ui import main as run_window
    return int(run_window() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
