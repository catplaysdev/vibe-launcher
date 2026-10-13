import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def test_checking_for_the_built_in_terminal_does_not_load_the_browser_engine():
    # Qt WebEngine maps about 200 MB of code; an idle launcher must not carry it until a terminal opens.
    probe = ("import sys; sys.path.insert(0, %r); from vibe_launcher import terminal; "
             "print(terminal.available() in (True, False), 'PySide6.QtWebEngineCore' in sys.modules)" % str(SRC))
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    assert result.stdout.split() == ["True", "False"], result.stderr
