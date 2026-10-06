"""Release helpers: the CHANGELOG section of one version (the GitHub release body) and a tag / version consistency check.

    python scripts/release_notes.py check v1.2.3        # tag matches pyproject.toml and CHANGELOG.md has the section
    python scripts/release_notes.py notes v1.2.3 OUT.md # write that section's body (without its heading)

The launcher's updater reads the body back with ``updates.parse_changelog``, so keep ``### 設定變更`` lines intact.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADING = re.compile(r"^##\s+\[([^\]]+)\]")


def section(text: str, version: str) -> str | None:
    """The lines under ``## [version]`` up to the next ``## [`` heading, or None when the version has no section."""
    found, body = False, []
    for line in text.splitlines():
        match = HEADING.match(line.strip())
        if match:
            if found:
                break
            found = match.group(1).strip() == version
            continue
        if found:
            body.append(line)
    return "\n".join(body).strip() + "\n" if found else None


def project_version(pyproject: str) -> str | None:
    match = re.search(r'(?m)^version\s*=\s*"(\d+\.\d+\.\d+)"', pyproject)
    return match.group(1) if match else None


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in {"check", "notes"} or not re.fullmatch(r"v\d+\.\d+\.\d+", argv[2]) \
            or (argv[1] == "notes" and len(argv) < 4):
        print(__doc__)
        return 2
    version = argv[2][1:]
    found = project_version((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    body = section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    if found != version:
        print(f"Tag {argv[2]} does not match pyproject.toml version {found}")
        return 1
    if not body or not body.strip():
        print(f"CHANGELOG.md has no notes under '## [{version}]'")
        return 1
    if argv[1] == "notes":
        Path(argv[3]).write_text(body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
