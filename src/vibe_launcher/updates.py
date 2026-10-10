"""Launcher self-update: find newer tagged versions (git checkout, or GitHub Releases for a packaged build) and apply them.

UI-side only (never shipped to SSH hosts). Two channels share one report format, cache and backup step:

* a git checkout: the checkout's tags are the releases and each tag's CHANGELOG.md describes it.
* a packaged build (``selfcmd.frozen()``): the GitHub Releases of ``RELEASE_REPO``. A release carries
  ``Vibe-Launcher-Setup-<version>.exe`` and ``SHA256SUMS.txt`` and its notes are that version's CHANGELOG section. Applying
  one downloads the installer in small chunks, checks its SHA-256, backs up user data and starts the installer, which
  closes the launcher and starts the new version. Nothing runs in the background between checks.

Only versions newer than the installed one are reported. Applying an update backs up the user's data first and never edits
settings: anything a release removes or changes is listed under ``### 設定變更`` in CHANGELOG.md so the user is warned
before updating. Each CHANGELOG section lists its notes in Traditional Chinese first and, after an ``### English`` heading,
again in English; update notes use Traditional Chinese only for Traditional Chinese system display languages,
and English for every other system. Progress messages follow the interface language.

    python -m vibe_launcher.updates check [--repo PATH] [--data PATH]
    python -m vibe_launcher.updates apply --repo PATH --data PATH
    vibe-launcher-cli updates apply-release --data PATH
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import runtime, selfcmd
from .i18n import tr

log = logging.getLogger(__name__)

RELEASE_REPO = "catplaysdev/vibe-launcher"
RELEASES_API = "https://api.github.com/repos/{repo}/releases?per_page=10"
DOWNLOAD_PREFIX = "https://github.com/{repo}/releases/download/"
TRUSTED_HOSTS = ("github.com", "api.github.com", "githubusercontent.com")      # downloads redirect to *.githubusercontent.com
SUMS_NAME = "SHA256SUMS.txt"
API_LIMIT = 1024 * 1024
SUMS_LIMIT = 64 * 1024
INSTALLER_LIMIT = 800 * 1024 * 1024
CHUNK = 256 * 1024                    # downloads are streamed to disk in pieces this size; memory use stays flat
UPDATES_DIR = "updates"
CACHE_NAME = "app-update.json"
PROGRESS_NAME = "update-progress.json"       # what the update window shows while apply / apply-release runs
CACHE_LIMIT = 262144
CHANGELOG_LIMIT = 256 * 1024
FETCH_TIMEOUT = 30
GIT_TIMEOUT = 15
MAX_RELEASES = 50
KEEP_BACKUPS = 10
USER_FILES = ("ui-state.json", "settings.json")
DATABASE_NAME = "vibe-launcher.sqlite3"
STATUSES = {"update_available", "current", "unavailable"}
TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
SEMVER = re.compile(r"\d+\.\d+\.\d+")
ENGLISH_HEADING = re.compile(r"^###\s+English\s*$", re.IGNORECASE)
SECTION = re.compile(r"^##\s+\[([^\]]+)\](?:\s*[-–—]\s*(\S+))?\s*$")
BACKUP_DIR = re.compile(r"^[0-9A-Za-z.+_-]+-(\d{8}T\d{6}Z)(?:-(\d+))?$")


def current_version() -> str:
    from . import __version__
    return __version__


def repo_root() -> Path | None:
    """The git checkout that contains this package, or None (a packaged build, an installed wheel)."""
    if selfcmd.frozen():
        return None
    for folder in Path(__file__).resolve().parents:
        if (folder / ".git").exists():
            return folder
    return None


def parse_changelog(text: str, *, include_unreleased: bool = False) -> dict[str, dict]:
    """``## [x.y.z] - date`` sections -> {version: {"date", "notes", "notes_en"}}: every bullet is a release note.

    Bullets before an ``### English`` heading are the (Chinese) ``notes``; the ones after it are ``notes_en``.
    The ``[Unreleased]`` section is skipped unless asked for.
    """
    releases: dict[str, dict] = {}
    entry: dict | None = None
    key = "notes"
    for line in text.splitlines():
        stripped = line.strip()
        match = SECTION.match(stripped)
        if match:
            name = match.group(1).strip()
            if SEMVER.fullmatch(name) or (include_unreleased and name.lower() == "unreleased"):
                entry = releases.setdefault(name, {"date": match.group(2) or "", "notes": [], "notes_en": []})
            else:
                entry = None
            key = "notes"
            continue
        if stripped.startswith("# ") and not stripped.startswith("## "):
            entry = None
            continue
        if entry is None:
            continue
        if stripped.startswith("###"):
            key = "notes_en" if ENGLISH_HEADING.match(stripped) else "notes"
            continue
        if not stripped.startswith(("- ", "* ")):
            continue
        item = stripped[2:].strip()
        if item:
            entry[key].append(item)
    return releases


def _system_language() -> str:
    """Windows display language, independent of the launcher's interface preference."""
    import locale
    if os.name == "nt":
        import ctypes
        try:
            return locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), "en")
        except (AttributeError, OSError):
            return "en"
    return locale.getlocale()[0] or "en"


def notes_for(release: dict, language: str | None = None) -> list[str]:
    """Traditional Chinese systems get Chinese notes; every other system gets English."""
    chinese, english = release.get("notes") or [], release.get("notes_en") or []
    parts = (language or _system_language()).replace("_", "-").lower().split("-")
    traditional = parts[0] == "zh" and "hans" not in parts and (
        "hant" in parts or any(region in parts[1:] for region in ("tw", "hk", "mo")))
    if traditional:
        return chinese or english
    if english:
        return english
    # Older English-only releases stored their bullets in notes, without an English heading.
    if chinese and not any(re.search(r"[\u3400-\u9fff]", note) for note in chinese):
        return chinese
    return ["English release notes are unavailable for this version."] if chinese else []


def release_text(releases: list[dict]) -> str:
    """Plain text of releases in both languages, for copying (e.g. into a translator)."""
    blocks = []
    for release in releases:
        lines = [f"v{release.get('version', '')} ({release.get('date', '')})".replace(" ()", "")]
        for title, key in (("繁體中文", "notes"), ("English", "notes_en")):
            if release.get(key):
                lines += [f"[{title}]"] + [f"- {note}" for note in release[key]]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _git_env() -> dict:
    # Never prompt: stored credentials still work, but no terminal question or credential-manager window appears.
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_ALL": "C"}


def _git(git: str, repo: Path, *args: str, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run([git, "-C", str(repo), *args], capture_output=True, timeout=timeout, env=_git_env(),
                          stdin=subprocess.DEVNULL, creationflags=flags, check=False)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tag_version(tag: str) -> tuple[int, int, int] | None:
    match = TAG.fullmatch(tag.strip())
    return tuple(map(int, match.groups())) if match else None


def check(repo: Path | None, data_root: Path, *, fetch: bool = True) -> dict:
    """Blocking check for newer tagged releases; the result is also cached in ``<data_root>/app-update.json``."""
    current = current_version()
    result = {"status": "unavailable", "current": current, "latest": current, "releases": [],
              "checked_at": _now(), "message": ""}
    try:
        result.update(_check(repo, current, fetch))
    except Exception:  # never let a broken checkout or network take the launcher down
        log.exception("Update check failed")
        result.update(status="unavailable", releases=[], message=tr("Something went wrong while checking for updates."))
    try:
        runtime.write_json(Path(data_root) / CACHE_NAME, result)
    except (OSError, ValueError):
        log.warning("Cannot cache the update check result")
    return result


def _check(repo: Path | None, current: str, fetch: bool) -> dict:
    if selfcmd.frozen():
        return _check_release(current)
    if repo is None or not (Path(repo) / ".git").exists():
        return {"message": tr("This copy is not a Git checkout, so it cannot check for updates by itself.")}
    try:
        git = runtime.trusted_executable("git")
    except RuntimeError:
        return {"message": tr("Git was not found, so updates cannot be checked.")}
    repo = Path(repo)
    if fetch:
        try:
            fetched = _git(git, repo, "fetch", "--tags", "--quiet", "origin", timeout=FETCH_TIMEOUT)
        except subprocess.TimeoutExpired:
            return {"message": tr("The update source did not answer in time. Try again later.")}
        if fetched.returncode != 0:
            return {"message": tr("Cannot reach the update source (git fetch failed).")}
    listed = _git(git, repo, "tag", "--list", "v*")
    if listed.returncode != 0:
        return {"message": tr("Cannot read the version tags.")}
    installed = runtime.version_number(current) or (0, 0, 0)
    tags = {}
    for line in listed.stdout.decode("utf-8", "replace").splitlines():
        number = _tag_version(line)
        if number and number > installed:
            tags[number] = line.strip()
    releases = []
    for number in sorted(tags, reverse=True)[:MAX_RELEASES]:
        version = "%d.%d.%d" % number
        entry = _release_notes(git, repo, tags[number]).get(version, {})
        releases.append({"version": version, "date": entry.get("date", ""), "notes": entry.get("notes", []),
                         "notes_en": entry.get("notes_en", [])})
    if not releases:
        return {"status": "current", "latest": current, "message": tr("You are on the latest version.")}
    return {"status": "update_available", "latest": releases[0]["version"], "releases": releases,
            "message": tr("Version {latest} can be installed.", latest=releases[0]["version"])}


class _SameSite(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to https pages of GitHub (release files are served from githubusercontent.com)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _trusted(newurl):
            raise urllib.error.URLError("Redirect to an untrusted address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _trusted(url: str) -> bool:
    match = re.fullmatch(r"https://([A-Za-z0-9.-]+)(?::443)?/.*", url)
    return bool(match) and any(match.group(1) == host or match.group(1).endswith("." + host) for host in TRUSTED_HOSTS)


def _open(url: str):
    if not _trusted(url):
        raise ValueError("Untrusted update address")
    request = urllib.request.Request(url, headers={"User-Agent": "Vibe-Launcher/" + current_version(),
                                                   "Accept": "application/vnd.github+json"})
    return urllib.request.build_opener(_SameSite).open(request, timeout=FETCH_TIMEOUT)


def _read(url: str, limit: int) -> bytes:
    with _open(url) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Update data is larger than expected")
    return data


def _latest_releases(repo: str, installed: tuple[int, int, int]) -> list[dict]:
    """Published releases newer than ``installed`` that can be installed, newest first (one small HTTPS request)."""
    listing = json.loads(_read(RELEASES_API.format(repo=repo), API_LIMIT))
    found = []
    for item in listing if isinstance(listing, list) else []:
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
            continue
        number = _tag_version(str(item.get("tag_name", "")))
        if not number or number <= installed:
            continue
        version = "%d.%d.%d" % number
        assets = {a.get("name"): a for a in item.get("assets", []) if isinstance(a, dict)}
        installer, sums = assets.get(f"Vibe-Launcher-Setup-{version}.exe"), assets.get(SUMS_NAME)
        prefix = DOWNLOAD_PREFIX.format(repo=repo)
        if not installer or not sums or not all(str(a.get("browser_download_url", "")).startswith(prefix) for a in (installer, sums)):
            continue            # not (yet) installable: the assets are uploaded after the release is created
        date = str(item.get("published_at") or "")[:10]
        notes = parse_changelog(f"## [{version}] - {date}\n" + str(item.get("body") or "")).get(version, {})
        size = installer.get("size")
        found.append({"version": version, "date": date, "notes": notes.get("notes", []), "notes_en": notes.get("notes_en", []),
                      "size": size if isinstance(size, int) and 0 < size <= INSTALLER_LIMIT else 0,
                      "installer_url": installer["browser_download_url"], "sums_url": sums["browser_download_url"]})
    return sorted(found, key=lambda r: runtime.version_number(r["version"]), reverse=True)[:MAX_RELEASES]


def _check_release(current: str) -> dict:
    try:
        found = _latest_releases(RELEASE_REPO, runtime.version_number(current) or (0, 0, 0))
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        return {"message": tr("Cannot reach the update source. Try again later.")}
    if not found:
        return {"status": "current", "latest": current, "message": tr("You are on the latest version.")}
    releases = [{k: r[k] for k in ("version", "date", "notes", "notes_en", "size")} for r in found]
    return {"status": "update_available", "latest": releases[0]["version"], "releases": releases,
            "message": tr("Version {latest} can be installed.", latest=releases[0]["version"])}


def _checksum(sums: bytes, name: str) -> str:
    for line in sums.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    raise ValueError("No checksum for the installer")


def _download(url: str, target: Path, expected: str, limit: int = INSTALLER_LIMIT, on_progress=None) -> None:
    """Stream ``url`` to ``target`` (a ``.part`` file first), checking size and SHA-256 before it gets its real name."""
    partial = target.with_name(target.name + ".part")
    digest, size = hashlib.sha256(), 0
    try:
        with _open(url) as response, open(partial, "xb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            step = 0
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ValueError("The download is larger than expected")
                digest.update(chunk)
                out.write(chunk)
                if on_progress is not None:
                    on_progress(size, total)
                if total and size * 10 // total > step:
                    step = size * 10 // total
                    print(f"    {step * 10}%", flush=True)
        if digest.hexdigest() != expected:
            raise ValueError("The downloaded file does not match its checksum")
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)


class Progress:
    """The update window's view of a running apply: ``<data>/update-progress.json``, written atomically.

    Display only. Whether the update worked is decided by the exit code, never by this file.
    """

    def __init__(self, data_root: Path, stages: list[str], to_version: str = "") -> None:
        self.path = Path(data_root) / PROGRESS_NAME
        self.data = {"stages": stages, "step": 0, "state": "running", "from": current_version(), "to": to_version,
                     "done": 0, "total": 0, "detail": "", "message": "", "pid": os.getpid(), "started": _now(), "at": _now()}
        self._last = 0.0
        self.write()

    def write(self, **fields) -> None:
        self.data.update(fields, at=_now())
        try:
            runtime.write_json(self.path, self.data)
        except (OSError, ValueError):
            log.warning("Cannot write the update progress")

    def stage(self, step: int, **fields) -> None:
        """Enter stage ``step`` (1-based) of the list and print its line."""
        print(f"{step}/{len(self.data['stages'])} {self.data['stages'][step - 1]}…", flush=True)
        self.write(**{"step": step, "done": 0, "total": 0, "detail": "", **fields})

    def bytes(self, done: int, total: int) -> None:
        now = time.monotonic()
        if now - self._last >= 0.25 or (total and done >= total):
            self._last = now
            self.write(done=done, total=total)

    def finish(self, state: str, message: str, code: int) -> int:
        print(message, flush=True)
        self.write(state=state, message=message)
        return code


def use_interface_language(data_root: Path) -> None:
    """Helper processes print in the language the launcher window uses (read from its ``ui-state.json``)."""
    from . import i18n
    try:
        value = json.loads(runtime.read_private(Path(data_root) / "ui-state.json")).get("language")
    except (OSError, ValueError, AttributeError):
        return
    if value in i18n.LANGUAGES:
        i18n.set_language(value)


def apply_release(data_root: Path) -> int:
    """Interactive update of a packaged build: newest release -> backup -> download + verify -> run the installer."""
    data_root = Path(data_root)
    use_interface_language(data_root)
    print("== " + tr("Vibe Launcher update") + " ==", flush=True)
    progress = Progress(data_root, [tr("Look for the new version"), tr("Back up your data"),
                                    tr("Download and verify the installer"), tr("Start the installer")])
    progress.stage(1)
    try:
        found = _latest_releases(RELEASE_REPO, runtime.version_number(current_version()) or (0, 0, 0))
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        return progress.finish("failed", tr("Cannot reach the update source. Nothing was changed."), 4)
    if not found:
        return progress.finish("current", tr("You are on the latest version."), 0)
    release = found[0]
    total = int(release.get("size") or 0)
    progress.write(to=release["version"], total=total, versions=[r["version"] for r in found])
    print("    " + tr("Latest version {version}", version=release["version"]), flush=True)
    progress.stage(2)
    try:
        folder = backup_user_data(data_root, current_version())
    except (OSError, ValueError, sqlite3.Error) as error:
        return progress.finish("failed", tr("The backup failed, so the update was stopped: {error}", error=type(error).__name__), 3)
    print("    " + tr("Backed up to {path}", path=folder))
    progress.write(detail=str(folder))
    progress.stage(3, total=total)
    name = f"Vibe-Launcher-Setup-{release['version']}.exe"
    downloads = data_root / UPDATES_DIR
    try:
        runtime.private_dir(downloads)
        for old in downloads.iterdir():
            if old.is_file() and not old.is_symlink():
                old.unlink(missing_ok=True)        # earlier downloads are never reused
        expected = _checksum(_read(release["sums_url"], SUMS_LIMIT), name)
        progress.write(detail=name)
        target = downloads / name
        _download(release["installer_url"], target, expected, on_progress=progress.bytes)
    except (OSError, ValueError, urllib.error.URLError, TimeoutError) as error:
        return progress.finish("failed", tr("The download or its check failed ({error}). Nothing was changed.",
                                            error=type(error).__name__), 4)
    size = target.stat().st_size
    progress.write(detail=tr("{name} (checksum verified)", name=name), done=size, total=size)
    progress.stage(4, done=size, total=size, detail=tr("{name} (checksum verified)", name=name))
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen([str(target), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                         creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL)
    except OSError:
        return progress.finish("failed", tr("The installer could not be started. The verified file is at {path}; you can run it by hand.",
                                            path=target), 5)
    return progress.finish("launched", tr("The installer has started: it closes Vibe Launcher, replaces the files and reopens it."), 0)


def _release_notes(git: str, repo: Path, tag: str) -> dict[str, dict]:
    shown = _git(git, repo, "show", f"{tag}:CHANGELOG.md")
    if shown.returncode != 0 or len(shown.stdout) > CHANGELOG_LIMIT:
        return {}
    return parse_changelog(shown.stdout.decode("utf-8", "replace"))


def _strings(value: object, limit: int = 200) -> bool:
    return isinstance(value, list) and len(value) <= limit and all(isinstance(v, str) and len(v) <= 2000 for v in value)


def _valid_release(item: object) -> bool:
    if not isinstance(item, dict) or not isinstance(item.get("version"), str) or not SEMVER.fullmatch(item["version"]):
        return False
    size = item.get("size", 0)
    return (isinstance(item.get("date"), str) and _strings(item.get("notes")) and _strings(item.get("notes_en", []))
            and isinstance(size, int) and not isinstance(size, bool) and 0 <= size <= INSTALLER_LIMIT)


def load_cached(data_root: Path) -> dict | None:
    """The last check result, or None when it is missing or malformed."""
    try:
        data = json.loads(runtime.read_private(Path(data_root) / CACHE_NAME, limit=CACHE_LIMIT))
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or data.get("status") not in STATUSES
            or not all(isinstance(data.get(k), str) for k in ("current", "latest", "checked_at", "message"))
            or not isinstance(data.get("releases"), list) or len(data["releases"]) > MAX_RELEASES
            or not all(_valid_release(r) for r in data["releases"])):
        return None
    return {k: data[k] for k in ("status", "current", "latest", "releases", "checked_at", "message")}


def working_tree_clean(repo: Path) -> tuple[bool, str]:
    """(True, "") when tracked files have no local changes; untracked/ignored files do not count."""
    try:
        git = runtime.trusted_executable("git")
        status = _git(git, Path(repo), "status", "--porcelain", "--untracked-files=no")
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return False, tr("Cannot run git status.")
    if status.returncode != 0:
        return False, tr("Cannot read the git status (not a Git checkout?).")
    changed = [line[3:] for line in status.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    if not changed:
        return True, ""
    shown = "\n".join("  " + path for path in changed[:10])
    more = "\n  " + tr("…and {count} more file(s)", count=len(changed) - 10) if len(changed) > 10 else ""
    return False, tr("{count} file(s) have uncommitted changes:", count=len(changed)) + f"\n{shown}{more}"


def backup_user_data(data_root: Path, version: str) -> Path:
    """Copy the database (SQLite online backup) and the UI / CLI settings into a new private backup folder."""
    data_root = Path(data_root)
    safe = re.sub(r"[^0-9A-Za-z.+_-]", "_", version)[:40] or "unknown"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups = data_root / "backups"
    runtime.private_dir(backups)
    # Same second: number after the highest existing one, so a pruned name is never reused for a newer backup.
    taken = [int(m.group(2) or 1) for child in backups.iterdir()
             if (m := BACKUP_DIR.match(child.name)) and m.group(1) == stamp]
    counter = max(taken, default=0) + 1
    target = backups / (f"{safe}-{stamp}" if counter == 1 else f"{safe}-{stamp}-{counter}")
    while target.exists():
        counter += 1
        target = backups / f"{safe}-{stamp}-{counter}"
    runtime.private_dir(target)
    database = data_root / DATABASE_NAME
    runtime.assert_private_path(database)
    if database.is_file():
        source = sqlite3.connect(database, timeout=5)
        destination = sqlite3.connect(target / DATABASE_NAME)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        runtime.protect_path(target / DATABASE_NAME)
    for name in USER_FILES:
        path = data_root / name
        runtime.assert_private_path(path)
        if path.is_file():
            shutil.copyfile(path, target / name)
            runtime.protect_path(target / name)
    _prune(backups)
    return target


def _prune(backups: Path, keep: int = KEEP_BACKUPS) -> None:
    folders = []
    for child in backups.iterdir():
        match = BACKUP_DIR.match(child.name)
        if match and child.is_dir() and not child.is_symlink():
            folders.append((match.group(1), int(match.group(2) or 1), child))
    folders.sort(key=lambda item: item[:2], reverse=True)
    for _, _, folder in folders[keep:]:
        try:
            runtime.assert_private_path(folder)
            shutil.rmtree(folder)
        except (OSError, ValueError):
            log.warning("Cannot remove old backup %s", folder.name)


def apply(repo: Path, data_root: Path) -> int:
    """Interactive update for an embedded terminal: clean tree -> backup -> warn -> pull --ff-only -> uv sync."""
    use_interface_language(data_root)
    print("== " + tr("Vibe Launcher update") + " ==", flush=True)
    progress = Progress(data_root, [tr("Check local changes"), tr("Back up your data"),
                                    tr("Download the update (git pull --ff-only)"), tr("Update dependencies (uv sync)")])
    if not (Path(repo) / ".git").exists():
        return progress.finish("failed", tr("No Git checkout was found, so it cannot update itself."), 1)
    try:
        git = runtime.trusted_executable("git")
    except RuntimeError:
        return progress.finish("failed", tr("Git was not found. Install Git for Windows first."), 1)
    progress.stage(1)
    clean, message = working_tree_clean(repo)
    if not clean:
        print(tr("The program folder has uncommitted changes; the update was stopped so they are not overwritten.") + "\n" + message)
        return progress.finish("failed", tr("Commit, stash (git stash) or discard these changes first, then try again."), 2)
    progress.stage(2)
    try:
        folder = backup_user_data(data_root, current_version())
    except (OSError, ValueError, sqlite3.Error) as error:
        return progress.finish("failed", tr("The backup failed, so the update was stopped: {error}", error=type(error).__name__), 3)
    print("    " + tr("Backed up to {path}", path=folder))
    progress.write(detail=str(folder))
    progress.stage(3)
    try:
        pulled = subprocess.call([git, "-C", str(repo), "pull", "--ff-only"], env=_git_env())
    except OSError:
        pulled = 1
    if pulled != 0:
        return progress.finish("failed", tr("The download failed (network, permission, or the local branch has diverged). Nothing was changed."), 4)
    progress.stage(4)
    try:
        uv = runtime.trusted_executable("uv")
    except RuntimeError:
        return progress.finish("failed", tr("uv was not found. The code is updated; install uv (https://docs.astral.sh/uv/) and run `uv sync` in the program folder."), 5)
    try:
        synced = subprocess.call([uv, "sync"], cwd=str(repo))
    except OSError:
        synced = 1
    if synced != 0:
        return progress.finish("failed", tr("`uv sync` failed. The code is updated; run `uv sync` in the program folder by hand."), 6)
    return progress.finish("done", tr("Update finished. Restart Vibe Launcher."), 0)


def _default_data_root() -> Path:
    from .service import data_root
    return data_root()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vibe_launcher.updates", description="Vibe Launcher updater")
    commands = parser.add_subparsers(dest="command", required=True)
    release_command = commands.add_parser("apply-release")
    release_command.add_argument("--data", type=Path, default=None)
    for name in ("check", "apply"):
        command = commands.add_parser(name)
        command.add_argument("--repo", type=Path, default=None)
        command.add_argument("--data", type=Path, default=None)
        if name == "check":
            command.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    data = args.data or _default_data_root()
    if args.command == "apply-release":
        return apply_release(data)
    repo = args.repo or repo_root()
    if args.command == "check":
        print(json.dumps(check(repo, data, fetch=not args.no_fetch), ensure_ascii=False, indent=2))
        return 0
    if repo is None:
        print(tr("No Git checkout was found, so it cannot update itself."))
        return 1
    return apply(repo, data)


if __name__ == "__main__":
    sys.exit(main())
