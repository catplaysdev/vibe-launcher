"""PyInstaller entry point: ``Vibe Launcher.exe`` (window) and ``vibe-launcher-cli.exe`` (helper jobs) both run this."""
import multiprocessing

from vibe_launcher.entry import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
