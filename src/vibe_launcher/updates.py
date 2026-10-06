"""Launcher self-update: find newer tagged versions (git checkout, or GitHub Releases for a packaged build) and apply them.

UI-side only (never shipped to SSH hosts). Two channels share one report format, cache and backup step:

* a git checkout: the checkout's tags are the releases and each tag's CHANGELOG.md describes it.
* a packaged build (``selfcmd.frozen()``): the GitHub Releases of ``RELEASE_REPO``. A release carries
  ``Vibe-Launcher-Setup-<version>.exe`` and ``SHA256SUMS.txt`` and its notes are that version's CHANGELOG section. Applying
  one downloads the installer in small chunks, checks its SHA-256, backs up user data and starts the installer, which
  closes the launcher and starts the new version. Nothing runs in the background between checks.

Only versions newer than the installed one are reported. Applying an update backs up the user's data first and never edits
settings: anything a release removes or changes is listed under ``### 設定變更`` in CHANGELOG.md so the user is warned
before updating.

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
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import runtime, selfcmd

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
SECTION = re.compile(r"^##\s+\[([^\]]+)\](?:\s*[-–—]\s*(\S+))?\s*$")
SETTINGS_HEADING = "設定變更"
CHANGE = re.compile(r"^(移除|變更)\s*[:：]\s*(.+?)\s*$")
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
    """``## [x.y.z] - date`` sections -> {version: {"date", "notes", "settings_changes"}}.

    Bullets under ``### 設定變更`` of the form ``- 移除: key`` / ``- 變更: key（說明）`` become settings changes;
    every other bullet is a release note. The ``[Unreleased]`` section is skipped unless asked for.
    """
    releases: dict[str, dict] = {}
    entry: dict | None = None
    in_settings = False
    for line in text.splitlines():
        stripped = line.strip()
        match = SECTION.match(stripped)
        if match:
            name = match.group(1).strip()
            in_settings = False
            if SEMVER.fullmatch(name) or (include_unreleased and name.lower() == "unreleased"):
                entry = releases.setdefault(name, {"date": match.group(2) or "", "notes": [], "settings_changes": []})
            else:
                entry = None
            continue
        if stripped.startswith("# ") and not stripped.startswith("## "):
            entry = None
            continue
        if entry is None:
            continue
        if stripped.startswith("###"):
            in_settings = stripped.lstrip("#").strip() == SETTINGS_HEADING
            continue
        if not stripped.startswith(("- ", "* ")):
            continue
        item = stripped[2:].strip()
        if not item:
            continue
        change = CHANGE.match(item) if in_settings else None
        if change:
            key, detail = _split_detail(change.group(2))
            entry["settings_changes"].append({"kind": "removed" if change.group(1) == "移除" else "changed",
                                              "key": key, "detail": detail})
        else:
            entry["notes"].append(item)
    return releases


def _split_detail(value: str) -> tuple[str, str]:
    for opening, closing in (("（", "）"), ("(", ")")):
        if value.endswith(closing) and opening in value:
            index = value.index(opening)
            return value[:index].strip(), value[index + 1:-1].strip()
    return value.strip(), ""


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
        result.update(status="unavailable", releases=[], message="檢查更新時發生錯誤。")
    try:
        runtime.write_json(Path(data_root) / CACHE_NAME, result)
    except (OSError, ValueError):
        log.warning("Cannot cache the update check result")
    return result


def _check(repo: Path | None, current: str, fetch: bool) -> dict:
    if selfcmd.frozen():
        return _check_release(current)
    if repo is None or not (Path(repo) / ".git").exists():
        return {"message": "此版本不是從 git checkout 執行，無法自動檢查更新。"}
    try:
        git = runtime.trusted_executable("git")
    except RuntimeError:
        return {"message": "找不到 git，無法檢查更新。"}
    repo = Path(repo)
    if fetch:
        try:
            fetched = _git(git, repo, "fetch", "--tags", "--quiet", "origin", timeout=FETCH_TIMEOUT)
        except subprocess.TimeoutExpired:
            return {"message": "連線更新來源逾時，請稍後再試。"}
        if fetched.returncode != 0:
            return {"message": "無法連線到更新來源（git fetch 失敗）。"}
    listed = _git(git, repo, "tag", "--list", "v*")
    if listed.returncode != 0:
        return {"message": "無法讀取版本標籤。"}
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
                         "settings_changes": entry.get("settings_changes", [])})
    if not releases:
        return {"status": "current", "latest": current, "message": "已是最新版本。"}
    return {"status": "update_available", "latest": releases[0]["version"], "releases": releases,
            "message": f"有新版本 {releases[0]['version']} 可以更新。"}


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
        found.append({"version": version, "date": date, "notes": notes.get("notes", []),
                      "settings_changes": notes.get("settings_changes", []),
                      "installer_url": installer["browser_download_url"], "sums_url": sums["browser_download_url"]})
    return sorted(found, key=lambda r: runtime.version_number(r["version"]), reverse=True)[:MAX_RELEASES]


def _check_release(current: str) -> dict:
    try:
        found = _latest_releases(RELEASE_REPO, runtime.version_number(current) or (0, 0, 0))
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        return {"message": "無法連線到更新來源，請稍後再試。"}
    if not found:
        return {"status": "current", "latest": current, "message": "已是最新版本。"}
    releases = [{k: r[k] for k in ("version", "date", "notes", "settings_changes")} for r in found]
    return {"status": "update_available", "latest": releases[0]["version"], "releases": releases,
            "message": f"有新版本 {releases[0]['version']} 可以更新。"}


def _checksum(sums: bytes, name: str) -> str:
    for line in sums.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    raise ValueError("No checksum for the installer")


def _download(url: str, target: Path, expected: str, limit: int = INSTALLER_LIMIT) -> None:
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
                if total and size * 10 // total > step:
                    step = size * 10 // total
                    print(f"    {step * 10}%", flush=True)
        if digest.hexdigest() != expected:
            raise ValueError("The downloaded file does not match its checksum")
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)


def apply_release(data_root: Path) -> int:
    """Interactive update of a packaged build: newest release -> backup -> download + verify -> run the installer."""
    print("== Vibe Launcher 更新 ==", flush=True)
    data_root = Path(data_root)
    print("1/4 查詢新版本…", flush=True)
    try:
        found = _latest_releases(RELEASE_REPO, runtime.version_number(current_version()) or (0, 0, 0))
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        print("無法連線到更新來源，程式與資料都沒有被修改。")
        return 4
    if not found:
        print("已是最新版本。")
        return 0
    release = found[0]
    print(f"    最新版本 {release['version']}", flush=True)
    print("2/4 備份使用者資料…", flush=True)
    try:
        folder = backup_user_data(data_root, current_version())
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"備份失敗，已停止更新：{type(error).__name__}")
        return 3
    print(f"    已備份到 {folder}")
    changes = settings_changes({"releases": found})
    if changes:
        print("注意：新版本會移除或變更下列設定（現有設定檔不會被改寫，但新版本可能不再使用）：")
        for change in changes:
            kind = "移除" if change["kind"] == "removed" else "變更"
            detail = f"（{change['detail']}）" if change["detail"] else ""
            print(f"    [{change['version']}] {kind}: {change['key']}{detail}")
    print("3/4 下載安裝程式並驗證…", flush=True)
    name = f"Vibe-Launcher-Setup-{release['version']}.exe"
    downloads = data_root / UPDATES_DIR
    try:
        runtime.private_dir(downloads)
        for old in downloads.iterdir():
            if old.is_file() and not old.is_symlink():
                old.unlink(missing_ok=True)        # earlier downloads are never reused
        expected = _checksum(_read(release["sums_url"], SUMS_LIMIT), name)
        target = downloads / name
        _download(release["installer_url"], target, expected)
    except (OSError, ValueError, urllib.error.URLError, TimeoutError) as error:
        print(f"下載或驗證失敗（{type(error).__name__}），程式與資料都沒有被修改。")
        return 4
    print("4/4 啟動安裝程式…", flush=True)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen([str(target), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                         creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL)
    except OSError:
        print(f"無法啟動安裝程式。已驗證的檔案在：{target}，可手動執行。")
        return 5
    print("安裝程式已啟動：它會關閉 Vibe Launcher、更新檔案後自動重新開啟。")
    return 0


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
    changes = item.get("settings_changes")
    return (isinstance(item.get("date"), str) and _strings(item.get("notes"))
            and isinstance(changes, list) and len(changes) <= 200
            and all(isinstance(c, dict) and c.get("kind") in {"removed", "changed"}
                    and isinstance(c.get("key"), str) and isinstance(c.get("detail"), str) for c in changes))


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


def settings_changes(result: dict | None) -> list[dict]:
    """Every settings removal/change across the pending releases, tagged with the version that makes it."""
    return [{**change, "version": release["version"]}
            for release in (result or {}).get("releases", []) for change in release.get("settings_changes", [])]


def working_tree_clean(repo: Path) -> tuple[bool, str]:
    """(True, "") when tracked files have no local changes; untracked/ignored files do not count."""
    try:
        git = runtime.trusted_executable("git")
        status = _git(git, Path(repo), "status", "--porcelain", "--untracked-files=no")
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return False, "無法執行 git status。"
    if status.returncode != 0:
        return False, "無法讀取 git 狀態（不是 git checkout？）。"
    changed = [line[3:] for line in status.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    if not changed:
        return True, ""
    shown = "\n".join("  " + path for path in changed[:10])
    more = f"\n  …另有 {len(changed) - 10} 個檔案" if len(changed) > 10 else ""
    return False, f"有 {len(changed)} 個檔案尚未提交：\n{shown}{more}"


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
    print("== Vibe Launcher 更新 ==", flush=True)
    if not (Path(repo) / ".git").exists():
        print("找不到 git checkout，無法自動更新。")
        return 1
    try:
        git = runtime.trusted_executable("git")
    except RuntimeError:
        print("找不到 git，請先安裝 Git for Windows。")
        return 1
    print("1/4 檢查本機修改…", flush=True)
    clean, message = working_tree_clean(repo)
    if not clean:
        print("程式目錄有未提交的變更，為避免覆蓋已停止更新。\n" + message)
        print("請先提交、暫存（git stash）或還原這些變更後再試。")
        return 2
    print("2/4 備份使用者資料…", flush=True)
    try:
        folder = backup_user_data(data_root, current_version())
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"備份失敗，已停止更新：{type(error).__name__}")
        return 3
    print(f"    已備份到 {folder}")
    changes = settings_changes(load_cached(data_root))
    if changes:
        print("注意：新版本會移除或變更下列設定（現有設定檔不會被改寫，但新版本可能不再使用）：")
        for change in changes:
            kind = "移除" if change["kind"] == "removed" else "變更"
            detail = f"（{change['detail']}）" if change["detail"] else ""
            print(f"    [{change['version']}] {kind}: {change['key']}{detail}")
    print("3/4 下載更新（git pull --ff-only）…", flush=True)
    try:
        pulled = subprocess.call([git, "-C", str(repo), "pull", "--ff-only"], env=_git_env())
    except OSError:
        pulled = 1
    if pulled != 0:
        print("下載更新失敗（可能是網路、權限，或本機分支已分岔）。程式與資料都沒有被修改。")
        return 4
    print("4/4 更新相依套件（uv sync）…", flush=True)
    try:
        uv = runtime.trusted_executable("uv")
    except RuntimeError:
        print("找不到 uv。程式碼已更新，請安裝 uv（https://docs.astral.sh/uv/）後在程式目錄執行：uv sync")
        return 5
    try:
        synced = subprocess.call([uv, "sync"], cwd=str(repo))
    except OSError:
        synced = 1
    if synced != 0:
        print("uv sync 失敗。程式碼已更新，請在程式目錄手動執行：uv sync")
        return 6
    print("更新完成，請重新啟動 Vibe Launcher。")
    return 0


def _default_data_root() -> Path:
    from .service import data_root
    return data_root()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vibe_launcher.updates", description="Vibe Launcher 更新工具")
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
        print("找不到 git checkout，無法自動更新。")
        return 1
    return apply(repo, data)


if __name__ == "__main__":
    sys.exit(main())
