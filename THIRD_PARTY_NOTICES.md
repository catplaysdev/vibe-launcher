# Third-party notices

Vibe Launcher itself is free software under the **GNU General Public License v3.0 or later** (see `LICENSE`). It uses the
following components, each under its own licence. Their licence texts ship with the packages (for the installer, inside
the install folder next to the program).

| Component | Used for | Licence |
| --- | --- | --- |
| [Qt 6 / PySide6](https://doc.qt.io/qtforpython-6/) (including Qt WebEngine, which contains Chromium and its own third-party code) | the user interface and the built-in terminal view | LGPL-3.0 (with GPL-3.0 as an option) |
| [xterm.js](https://github.com/xtermjs/xterm.js) and its fit / unicode11 / webgl add-ons (`src/vibe_launcher/assets/xterm/`) | terminal emulation | MIT (`LICENSE-xterm.txt`) |
| [Python](https://www.python.org/) runtime | runs the launcher | PSF License |
| [PyInstaller](https://pyinstaller.org/) (build tool; its bootloader is part of the packaged program) | packaging | GPL-2.0-or-later with the PyInstaller bootloader exception |
| [Inno Setup](https://jrsoftware.org/isinfo.php) (build tool; not shipped) | the Windows installer | Inno Setup License |

Notes

* The packaged program is a folder of separate files (`onedir`): the Qt libraries stay replaceable DLLs, as the LGPL
  requires. Do not switch to a single-file build without checking this again.
* The Codex CLI and Claude Code are **not** part of Vibe Launcher and are not distributed with it; they are installed and
  signed in by the user with their official installers.
* Brand logos of those tools are not shipped (`src/vibe_launcher/assets/brand/` is ignored by git and left out of builds).
