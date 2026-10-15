"""Hook and status line command of a managed Claude Code session (see ``runtime.claude_hook``)."""
from __future__ import annotations

import sys

from . import runtime


def main() -> int:
    return runtime.claude_hook(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
