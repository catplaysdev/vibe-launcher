"""Standalone stdlib runtime, also deployed verbatim to the SSH host.

No credentials travel in requests or status events. Only this process and the
official CLIs touch the dedicated profile credentials.
"""
from __future__ import annotations

import base64
import copy
import ctypes
import getpass
import hashlib
import hmac
import ipaddress
import json
import math
import os
import queue
import re
import secrets
import select
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid
import unicodedata
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Mapping
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

WINDOWS = os.name == "nt"
TOOL_CAPABILITIES = {
    "codex": {"kind": "cli", "quota": True},
    "claude": {"kind": "cli", "quota": False},
    "deepseek-harness": {"kind": "web", "quota": False, "experimental": True},
}
_MESSAGES = {}


def tr(text: str, **values) -> str:
    """Standalone runtime translations are injected by the local launcher."""
    translated = _MESSAGES.get(text, text)
    return translated.format(**values) if values else translated

def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("Invalid identifier")
    return value


def profile_dir(root: Path, account: dict) -> Path:
    tool = account["tool"]
    if tool not in {"codex", "claude"}:
        raise ValueError("Unsupported CLI")
    return root / "profiles" / tool / safe_id(account["id"])


def assert_private_path(path: Path) -> None:
    """Reject links/reparse points before touching launcher-owned files."""
    assert_private_paths((path,))


def assert_private_paths(paths) -> None:
    """``assert_private_path`` for several files; folders they share are looked at once."""
    seen: set = set()
    for path in paths:
        path = Path(os.path.abspath(path))
        for part in (path, *path.parents):
            if part in seen:
                break                          # this folder, and every folder above it, was already checked
            seen.add(part)
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue  # e.g. SQLite's -wal/-shm files, which other launcher processes create and delete at any moment
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Launcher data cannot use symlinks or reparse points")
        try:
            info = path.stat()                 # one look at the file, so it cannot vanish between "exists" and "stat"
        except FileNotFoundError:
            continue
        if os.name != "nt" and info.st_uid != os.getuid():
            raise ValueError("Launcher data has a different owner")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:  # 0 only appears while a file is being deleted
            raise ValueError("Launcher data cannot use hard links")


@lru_cache(maxsize=1)
def windows_user_sid() -> str:
    from ctypes import wintypes
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    token = wintypes.HANDLE()
    api.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    api.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    api.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    if not api.OpenProcessToken(wintypes.HANDLE(-1), 8, ctypes.byref(token)):
        raise OSError("Cannot identify Windows user")
    try:
        size = wintypes.DWORD()
        api.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not api.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise OSError("Cannot identify Windows user")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not api.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise OSError("Cannot identify Windows user")
        try:
            return text.value
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def protect_path(path: Path) -> None:
    assert_private_path(path)
    if os.name != "nt":
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    # Owner + SYSTEM only; OI/CI propagates protection to newly created children.
    from ctypes import wintypes
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    descriptor = ctypes.c_void_p()
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    api.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    flags = "OICI" if path.is_dir() else ""
    sddl = f"D:P(A;{flags};FA;;;{windows_user_sid()})(A;{flags};FA;;;SY)"
    if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise OSError("Cannot create private data ACL")
    try:
        if not api.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise OSError("Cannot restrict launcher data ACL")
    finally:
        kernel = ctypes.WinDLL("Kernel32.dll")
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(descriptor)


def private_dir(path: Path) -> None:
    assert_private_path(path)
    if not path.parent.exists():
        private_dir(path.parent)
    path.mkdir(exist_ok=True, mode=0o700)
    protect_path(path)


def read_private(path: Path, limit: int = 1048576) -> str:
    assert_private_path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Invalid launcher data file")
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("Launcher file must be private to its owner")
        result = stream.read(limit + 1)
        if len(result) > limit:
            raise ValueError("Launcher data exceeds size limit")
        return result


def write_json(path: Path, data: dict) -> None:
    private_dir(path.parent)
    assert_private_path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            protect_path(temporary)
            json.dump(data, stream, ensure_ascii=False)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_endpoint(account: dict) -> None:
    url = account.get("base_url", "")
    if type(account.get("allow_insecure_loopback", False)) is not bool:
        raise ValueError("Invalid loopback opt-in")
    if not url:
        return
    parsed = urlsplit(url)
    _ = parsed.port
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
            or "?" in url or "#" in url or any(unicodedata.category(c) == "Cc" for c in url)):
        raise ValueError("Invalid API URL")
    try:
        loopback = parsed.hostname.lower() == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = False
    if parsed.scheme == "http" and not (account.get("allow_insecure_loopback") is True
            and loopback and "%" not in parsed.hostname):
        raise ValueError("API requires HTTPS; HTTP loopback needs explicit opt-in")


def validate_request(request: dict) -> None:
    locale = request.get("language", "en")
    if not isinstance(locale, str) or len(locale) > 32 or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", locale):
        raise ValueError("Invalid language")
    if request.get("action") not in {"codex-control", "doctor", "login", "launch", "list-sessions", "attach", "stop", "worker", "check-tools", "update-tool", "quota", "browse", "github", "harness", "import-settings", "import-history"}:
        raise ValueError("Unsupported action")
    if request.get("action") == "update-tool" and request.get("tool") not in {"codex", "claude"}:
        raise ValueError("Select Codex or Claude")
    if request.get("snap_channel", "") not in {"", "latest/stable", "latest/edge"}:
        raise ValueError("Unsupported Snap channel")
    if "persistent" in request and type(request["persistent"]) is not bool:
        raise ValueError("Invalid persistent flag")
    if "resume" in request and (type(request["resume"]) is not bool or request.get("action") not in {"launch", "worker"}):
        raise ValueError("Invalid resume flag")
    if request.get("mode", "agent") not in {"agent", "shell"}:
        raise ValueError("Unsupported session mode")
    if "terminal_profile" in request:
        if request.get("mode") != "shell" or request.get("action") not in {"launch", "worker"}:
            raise ValueError("Terminal profiles are only available for terminal launches")
        validate_terminal_profile(request["terminal_profile"])
    for key in ("session_id", "environment_id", "operation_id"):
        if request.get(key):
            safe_id(request[key])
    if "session_ids" in request:
        ids = request["session_ids"]
        if request.get("action") != "stop" or not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            raise ValueError("Invalid session list")
        for item in ids:
            safe_id(item)
    if request.get("account"):
        account = request["account"]
        if account.get("tool") not in TOOL_CAPABILITIES:
            raise ValueError("Unsupported tool")
        safe_id(account["id"])
        if account["tool"] == "deepseek-harness" and request["action"] != "harness":
            raise ValueError("Harness uses its official Web service")
        if account["tool"] != "deepseek-harness":
            profile_dir(Path("."), account)
        if account.get("auth") not in {"subscription", "api"}:
            raise ValueError("Unsupported authentication")
        for key in ("model", "base_url"):
            value = account.get(key, "")
            if not isinstance(value, str) or any(ord(c) < 32 for c in value):
                raise ValueError("Invalid account setting")
        url = account.get("base_url", "")
        validate_endpoint(account)
        if account["auth"] == "subscription" and url:
            raise ValueError("Subscription cannot override API URL")
        if account["auth"] == "api" and not account.get("model"):
            raise ValueError("API profile requires a model")
    path = request.get("path", "")
    if not isinstance(path, str) or any(ord(c) < 32 for c in path):
        raise ValueError("Invalid project path")
    if "project_history" in request and (type(request["project_history"]) is not bool or request["action"] != "import-history"):
        raise ValueError("Invalid project history flag")
    github = request.get("github_account")
    if github:
        safe_id(github["id"])
        if github.get("environment_id") != request.get("environment_id"):
            raise ValueError("GitHub account belongs to another environment")
    if request["action"] == "github" and not github:
        raise ValueError("Select a GitHub account")
    if request["action"] == "harness" and (not request.get("account") or not account_fits(request["account"], request.get("environment_id", ""))):
        raise ValueError("Select a Harness account bound to this environment")
    if request.get("resume_thread_id"):
        safe_id(request["resume_thread_id"])
        if request["action"] not in {"launch", "worker"} or not switch_supported(request.get("account", {})):
            raise ValueError("Invalid exact conversation resume")
    if "workspace_id" in request:
        if request["action"] not in {"launch", "worker", "stop"} or not isinstance(request["workspace_id"], str):
            raise ValueError("Invalid workspace")
        safe_id(request["workspace_id"] or "default")
    if "claude_hook_command" in request:
        command = request["claude_hook_command"]
        if (request["action"] not in {"launch", "worker"} or request.get("account", {}).get("tool") != "claude"
                or not isinstance(command, str) or not 0 < len(command) <= 2048 or not command.isprintable()):
            raise ValueError("Invalid Claude hook command")
    if "quota_policy" in request or "quota_accounts" in request:
        # A reopened terminal brings its saved flow; the worker applies it before any account is selected.
        if request["action"] not in {"launch", "worker"} or not switch_supported(request.get("account", {})):
            raise ValueError("Quota management requires a Codex or Claude subscription launch")
        environment = request.get("environment_id") or "local"
        seen = validate_control_accounts(request.get("quota_accounts", []), environment)
        policy = QuotaPolicy.from_dict(request.get("quota_policy"))
        if policy.environment_id != environment or not set(policy.account_ids) <= seen:
            raise ValueError("Quota policy accounts do not belong to this environment")
        if "quota_cursor" in request and request["quota_cursor"] not in policy.account_ids:
            raise ValueError("Invalid round-robin position")
    elif "quota_cursor" in request:
        raise ValueError("Invalid round-robin position")
    if request["action"] == "codex-control":
        validate_codex_control(request)


def operation_result(request: dict, root: Path, result: dict) -> None:
    if not request.get("operation_id"):
        return
    output = {"action": request["action"], "operation_id": request["operation_id"], "result": result}
    write_json(root / "results" / (safe_id(request["operation_id"]) + ".json"), output)
    print("VIBE_RESULT_" + request["nonce"] + ":" + json.dumps(output, ensure_ascii=True), flush=True)


def isolated_env(account: dict, root: Path, parent: dict | None = None) -> dict:
    env = dict(os.environ if parent is None else parent)
    # Remove routing/auth and nesting inherited from another agent. Preserve PATH,
    # HOME, SSH_AUTH_SOCK and normal development environment variables.
    for key in list(env):
        upper = key.upper()
        if (upper.startswith(("ANTHROPIC_", "OPENAI_", "CLAUDE_", "CODEX_"))
                or upper.startswith(("VIBE_SSH_", "PYTHON"))
                or upper in {"CLAUDECODE", "API_TIMEOUT_MS", "VIBE_API_KEY", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"}):
            del env[key]
    directory = str(profile_dir(root, account))
    env["CODEX_HOME" if account["tool"] == "codex" else "CLAUDE_CONFIG_DIR"] = directory
    if account["tool"] == "claude" and account["auth"] == "api":
        env["ANTHROPIC_BASE_URL"] = account.get("base_url") or "https://api.anthropic.com"
        env["ANTHROPIC_MODEL"] = account["model"]
    return env


def _safe_user_dir(path: Path, uid: int, info=None) -> bool:
    """A per-user install directory is trusted only if it is a real directory owned by us that nobody else can write."""
    try:
        info = info or path.stat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == uid and not info.st_mode & 0o022


def _node_version(name: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in name.lstrip("v").split("."))
    except ValueError:
        return (0,)


def user_tool_dirs(home: Path | None = None, *, uid: int | None = None) -> list[str]:
    """Per-user locations where CLIs are commonly installed but that an SSH non-interactive PATH usually lacks.

    Searched *after* the system PATH, so system installs win. Windows uses known native installer locations.
    On Unix, only directories owned by the current user and not group/world-writable qualify.
    """
    if WINDOWS:
        home = home or Path.home()
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        # Desktop shortcuts can inherit an old PATH after a native CLI install.
        found = [home / ".local" / "bin", local / "Programs" / "OpenAI" / "Codex" / "bin"]
        return [str(path) for path in found if path.is_absolute() and path.is_dir()]
    if uid is None:
        if not hasattr(os, "getuid"):
            return []
        uid = os.getuid()
    home = home or Path.home()
    found = [home / ".local" / "bin", home / ".npm-global" / "bin", home / ".bun" / "bin", home / ".volta" / "bin",
             home / ".cargo" / "bin", home / "bin"]
    nvm = sorted((home / ".nvm" / "versions" / "node").glob("*/bin"), key=lambda p: _node_version(p.parent.name), reverse=True)
    return [str(path) for path in found + nvm if _safe_user_dir(path, uid)]


def augmented_path(current: str | None = None, extras: list[str] | None = None) -> str:
    """System PATH first, then the safe per-user tool directories (no duplicates)."""
    parts = [p for p in (os.environ.get("PATH", "") if current is None else current).split(os.pathsep) if p]
    for directory in user_tool_dirs() if extras is None else extras:
        if directory not in parts:
            parts.append(directory)
    return os.pathsep.join(parts)


def is_shell(request: dict) -> bool:
    """A "shell" session opens the user's own login shell in the project folder instead of an agent CLI."""
    return request.get("mode") == "shell"


def shell_env(parent: dict | None = None) -> dict:
    """Environment for a plain shell: normal development variables, minus launcher plumbing and askpass helpers."""
    env = dict(os.environ if parent is None else parent)
    for key in list(env):
        if key.upper().startswith(("VIBE_", "SSH_ASKPASS")):
            del env[key]
    return env


def validate_terminal_profile(profile: dict) -> None:
    if not isinstance(profile, dict) or profile.keys() - {"id", "label", "platforms", "command", "profile_env"}:
        raise ValueError("Invalid terminal profile fields")
    safe_id(profile.get("id"))
    label = profile.get("label")
    if not isinstance(label, str) or not label.strip() or len(label) > 80 or any(ord(c) < 32 for c in label):
        raise ValueError("Invalid terminal profile label")
    platforms = profile.get("platforms")
    if not isinstance(platforms, list) or not platforms or any(not isinstance(p, str) or p not in {"windows", "linux"} for p in platforms):
        raise ValueError("Invalid terminal profile platforms")
    command = profile.get("command")
    if (not isinstance(command, list) or not 1 <= len(command) <= 32 or
            any(not isinstance(arg, str) or len(arg) > 1024 or any(ord(c) < 32 for c in arg) for arg in command) or
            not command[0].strip() or len(command[0]) > 512):
        raise ValueError("Terminal command must be an executable and an argument array")
    if not (os.path.isabs(command[0]) or ("/" not in command[0] and "\\" not in command[0])):
        raise ValueError("Terminal executable must be an absolute path or a PATH executable name")
    name = profile.get("profile_env")
    if name is not None and (not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name) or
            not name.endswith(("_HOME", "_CONFIG_DIR")) or name in {"HOME", "USERPROFILE", "XDG_CONFIG_HOME"}):
        raise ValueError("profile_env must be the CLI's dedicated *_HOME or *_CONFIG_DIR variable")


def shell_command(profile: dict | None = None) -> list[str]:
    if profile is not None:
        validate_terminal_profile(profile)
        if ("windows" if WINDOWS else "linux") not in profile["platforms"]:
            raise ValueError("Terminal profile is incompatible with this platform")
        return [trusted_executable(profile["command"][0]), *profile["command"][1:]]
    if WINDOWS:
        for name in ("pwsh", "powershell"):
            try:
                return [trusted_executable(name), "-NoLogo"]
            except RuntimeError:
                continue
        return [trusted_executable("cmd")]
    shell = os.environ.get("SHELL", "")
    if not (os.path.isabs(shell) and os.path.isfile(shell) and os.access(shell, os.X_OK)):
        shell = trusted_executable("bash")
    return [shell, "-l"]  # a login shell, so the user's own PATH/nvm/etc. setup applies


def trusted_executable(name: str) -> str:
    if Path(name).is_absolute():
        if not Path(name).is_file() or not os.access(name, os.X_OK):
            raise RuntimeError("Required executable is unavailable")
        return os.path.abspath(name)
    # Do not let Windows' implicit cwd search or a relative PATH entry select a
    # project-supplied executable. Native Windows CLI installers supply .exe.
    suffixes = (("",) if name.lower().endswith(".exe") else (".exe",)) if os.name == "nt" else ("",)
    for directory in os.get_exec_path():
        path = Path(directory)
        if not path.is_absolute() or path.resolve() == Path.cwd().resolve():
            continue
        for suffix in suffixes:
            candidate = path / (name + suffix)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                # Keep the executable's basename: Snap/multicall launchers select
                # the application from argv[0]. Absolute does not mean realpath.
                return os.path.abspath(candidate)
    raise RuntimeError("Required native executable is unavailable: " + name)


def cli_command(account: dict, action: str, root: Path, method: str = "device", project: Path | None = None,
                resume: bool = False, thread_id: str = "") -> list[str]:
    command = build_cli(account, action, root, method, project, resume, thread_id)
    command[0] = trusted_executable(command[0])
    if account["tool"] == "codex" and action == "launch" and "--no-daemon" in command:
        result = subprocess.run([command[0], "--help"], env=isolated_env(account, root),
                                cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if result.returncode or b"Codex CLI" not in result.stdout:
            raise RuntimeError("Cannot identify Codex CLI options; check codex --help in this environment")
        if b"--no-daemon" not in result.stdout.split():
            command.remove("--no-daemon")
    return command


def build_cli(account: dict, action: str, root: Path, method: str = "device", project: Path | None = None,
              resume: bool = False, thread_id: str = "") -> list[str]:
    tool = account["tool"]
    if tool not in {"codex", "claude"}:
        raise ValueError("Unknown terminal tool")
    if action == "login":
        if tool == "claude":
            return [tool, "auth", "login", "--claudeai"]
        return [tool, "login"] + (["--device-auth"] if method == "device" else [])
    if action == "status":
        return [tool, "login", "status"] if tool == "codex" else [tool, "auth", "status", "--json"]
    if tool == "claude":
        command = [tool]
        if account.get("model"):
            command += ["--model", account["model"]]
        for plugin in user_plugin_dirs(account, project=project):
            command += ["--plugin-dir", plugin]
        if resume and resumable(account, root, project):
            command.append("--continue")       # pick up the last conversation in this folder instead of a new one
        return command
    command = [tool, "--no-daemon"]
    native_state = native_codex_state()
    if native_state is not None:
        command += ["-c", "sqlite_home=" + json.dumps(str(native_state))]
    if account.get("model"):
        command += ["--model", account["model"]]
    if account["auth"] == "api":
        settings = {
            "model_provider": "vibe",
            "model_providers.vibe.name": "Vibe API",
            "model_providers.vibe.base_url": account.get("base_url") or "https://api.openai.com/v1",
            "model_providers.vibe.env_key": "VIBE_API_KEY",
            "model_providers.vibe.wire_api": "responses",
        }
        for key, value in settings.items():
            command += ["-c", f"{key}={json.dumps(value)}"]
        command += ["-c", "model_providers.vibe.requires_openai_auth=false"]
    else:
        command += ["-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"']
    if thread_id:
        command += ["resume", safe_id(thread_id)]      # the exact conversation, never a guess
    elif resume and resumable(account, root, project):
        command += ["resume", "--last"]        # the most recent session of this folder (Codex filters by cwd)
    return command


def resumable(account: dict, root: Path, project: Path | None) -> bool:
    """Is there an earlier conversation of this account in ``project`` to continue? (Claude refuses --continue without one.)"""
    if project is None:
        return False
    try:
        profile = profile_dir(root, account)
        if account["tool"] == "claude":
            folder = profile / "projects" / claude_project_folder(str(project))
            return folder.is_dir() and any(path.suffix == ".jsonl" for path in folder.iterdir())
        native = native_codex_state()
        rollouts = []
        for base in [profile / "sessions"] + ([native / "sessions"] if native is not None else []):
            if base.is_dir():
                rollouts += base.glob("*/*/*/rollout-*.jsonl")
        newest = sorted(rollouts, key=lambda path: path.name, reverse=True)[:200]
        return any(_same_project_path(_codex_rollout_cwd(path), str(project)) for path in newest)
    except (OSError, ValueError):
        return False


def check_settings(account: dict, root: Path, project: Path) -> None:
    """Refuse conflicting Claude routing settings; keep permissions/hooks intact."""
    if account["tool"] != "claude":
        return
    files = [profile_dir(root, account) / "settings.json"]
    for directory in (project, *project.parents):
        files.extend(directory / ".claude" / name for name in ("settings.json", "settings.local.json"))
    for path in files:
        if not path.is_file():
            continue
        try:
            settings = json.loads(path.read_text(encoding="utf-8-sig"))
            env = settings.get("env", {})
            conflict = bool(settings.get("apiKeyHelper")) or any(
                key.upper().startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_", "CLAUDE_CODE_OAUTH_"))
                for key in env)
        except (ValueError, AttributeError, TypeError):
            raise ValueError("Invalid Claude settings JSON; repair the settings file before launching") from None
        if conflict:
            raise ValueError("Claude settings contain provider/auth overrides. Remove those overrides before using this profile.")


def windows_secret(account: dict, value: str | None = None) -> str | None:
    """Windows Credential Manager generic credential, scoped to this app."""
    from ctypes import wintypes

    class Credential(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    target = "VibeLauncher/" + account["tool"] + "/" + safe_id(account["id"])
    if value is not None:
        raw = value.encode("utf-8")
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        credential = Credential(Type=1, TargetName=target, CredentialBlobSize=len(raw),
                                CredentialBlob=blob, Persist=2, UserName="VibeLauncher")
        api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise OSError("Cannot store API key in Windows Credential Manager")
        return value
    pointer = ctypes.POINTER(Credential)()
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.POINTER(ctypes.POINTER(Credential))]
    api.CredFree.argtypes = [ctypes.c_void_p]
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == 1168:
            return None
        raise OSError("Cannot read Windows Credential Manager")
    try:
        return ctypes.string_at(pointer.contents.CredentialBlob,
                                pointer.contents.CredentialBlobSize).decode("utf-8")
    finally:
        api.CredFree(pointer)


def api_key(account: dict, root: Path, replace: bool = False) -> str:
    validate_endpoint(account)
    path = profile_dir(root, account) / "api-key"
    binding = hashlib.sha256(json.dumps([account["tool"], account.get("base_url", ""),
                                        account.get("credential_revision", "")]).encode()).hexdigest()
    current = None
    if not replace:
        raw = windows_secret(account) if WINDOWS else (read_private(path) if path.exists() else None)
        try:
            saved = json.loads(raw) if raw else {}
            if saved.get("binding") == binding:
                current = saved.get("key")
        except (ValueError, AttributeError):
            pass  # unreadable: ask for the key again
    if current:
        return current
    if not sys.stdin.isatty():
        raise RuntimeError("API key setup requires an interactive terminal")
    value = getpass.getpass("API key (hidden; leave empty to cancel): ").strip()
    if not value or any(ord(c) < 32 for c in value):
        raise ValueError("API key setup cancelled or invalid")
    if WINDOWS:
        windows_secret(account, json.dumps({"binding": binding, "key": value}))
    else:
        write_json(path, {"binding": binding, "key": value})
    return value


def emit(request: dict, event: dict) -> None:
    event["updated_at"] = now()
    if request.get("action") == "worker":
        return      # a tmux worker writes into the window the user looks at; nothing reads events there
    print("\nVIBE_EVENT_" + request.get("nonce", "") + ":" + json.dumps(event), flush=True)


# --- sharing the user's own skills / hooks / agents with an isolated profile -------------------------------------
# A profile has its own CLAUDE_CONFIG_DIR / CODEX_HOME, so the user's ~/.claude and ~/.codex are invisible to it.
# We copy (never link: launcher data may not contain links) only the *extension* parts, never credentials.
# This file is also run on SSH hosts as a single script, so everything it needs lives here.
SHARE_DIRS = {"claude": ("skills", "agents", "commands", "hooks", "output-styles"),
              "codex": ("skills", "prompts", "agents", "rules", "plugins")}
SHARE_FILES = {"claude": (), "codex": ("hooks.json",)}      # plus every top-level *.md (CLAUDE.md, AGENTS.md, files they @import)
SHARE_SETTINGS = ("model", "hooks", "permissions", "statusLine", "outputStyle", "theme", "effortLevel", "attribution",
                  "autoContinueAtUsageLimit")
SHARE_TOML_KEYS = ("model", "approval_policy", "sandbox_mode", "model_reasoning_effort", "personality", "notify",
                   "file_opener", "hide_agent_reasoning", "developer_instructions")
SHARE_TOML_TABLES = ("mcp_servers", "features", "hooks", "skills", "agents", "plugins", "marketplaces", "tui",
                     "shell_environment_policy", "sandbox_workspace_write")
SHARE_MANIFEST = ".vibe-shared.json"
SHARE_MAX_FILE = 2 * 1024 * 1024
SHARE_MAX_FILES = 3000
SECRETISH = re.compile(r"(key|token|secret|passw|credential|auth)", re.I)
NEVER_COPY = {".credentials.json", "auth.json", ".env", "credentials.json", "id_rsa", "id_ed25519"}


def user_config_dir(tool: str, home: Path | None = None) -> Path:
    return (home or Path.home()) / (".claude" if tool == "claude" else ".codex")


def native_codex_state(home: Path | None = None) -> Path | None:
    """Use Codex's existing index, including paginated conversations, without sharing auth.json."""
    source = user_config_dir("codex", home)
    try:
        assert_private_path(source)
        if source.is_dir():
            for path in source.glob("state_*.sqlite"):
                assert_private_path(path)
                if path.is_file():
                    return source
    except (OSError, ValueError):
        pass                                   # a linked or unreadable ~/.codex: keep the profile's own index
    return None


def _plain(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return not (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400)


def _shareable_files(source: Path, tool: str, *, follow_skill_links: bool = False) -> list[Path]:
    """Relative paths of the files to copy: whitelisted folders and top-level notes, without links or secrets."""
    found: list[Path] = []
    if not source.is_dir():
        return found
    for entry in sorted(source.iterdir()):
        if entry.suffix.lower() == ".md" and entry.is_file() and _plain(entry):
            found.append(Path(entry.name))
        elif entry.name in SHARE_FILES[tool] and entry.is_file() and _plain(entry):
            found.append(Path(entry.name))
    for name in SHARE_DIRS[tool]:
        base = source / name
        linked_skills = follow_skill_links and name == "skills"
        if not base.is_dir() or (not linked_skills and not _plain(base)):
            continue
        visited = set()
        for current, dirs, files in os.walk(base, followlinks=linked_skills):
            here = Path(current)
            resolved = here.resolve()
            if resolved in visited:
                dirs[:] = []
                continue
            visited.add(resolved)
            # Directly under skills/, dot folders are the CLI's own state (Codex .system, Claude .trash) and Claude's
            # synced/ holds what its signed-in account synced: neither is copied to other profiles nor compared.
            top = here == base and name == "skills"
            dirs[:] = sorted(d for d in dirs if (linked_skills or _plain(here / d))
                             and d not in {".git", "node_modules", "__pycache__", ".system"}
                             and not (top and (d.startswith(".") or (tool == "claude" and d == "synced"))))
            for file in sorted(files):
                path = here / file
                if file.lower() in NEVER_COPY or (not linked_skills and not _plain(path)):
                    continue
                try:
                    if path.stat().st_size > SHARE_MAX_FILE:
                        continue
                except OSError:
                    continue
                found.append(path.relative_to(source))
                if len(found) > SHARE_MAX_FILES:
                    return found               # the caller decides: an import refuses, a launch copies what fits
    return found


def shared_settings(source: dict) -> tuple[dict, dict]:
    """The part of a user's Claude settings that is safe to hand to a profile: (settings, env without secrets/routing)."""
    result = {key: source[key] for key in SHARE_SETTINGS if key in source}
    env = source.get("env")
    kept = {}
    if isinstance(env, dict):
        for name, value in env.items():
            upper = str(name).upper()
            if (upper.startswith(("ANTHROPIC_", "OPENAI_", "CLAUDE_CODE_USE_", "CLAUDE_CODE_OAUTH_", "VIBE_", "CODEX_"))
                    or upper == "CLAUDE_CONFIG_DIR" or SECRETISH.search(upper) or not isinstance(value, (str, int, float, bool))):
                continue
            kept[str(name)] = value
    return result, kept


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()) + " }"
    return json.dumps(str(value), ensure_ascii=False)       # dates and anything unusual become strings


def toml_dumps(data: dict, prefix: str = "") -> str:
    lines = [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in data.items() if not isinstance(v, dict)]
    out = ("[" + prefix + "]\n" if prefix and lines else "") + "\n".join(lines) + ("\n" if lines else "")
    for key, value in data.items():
        if isinstance(value, dict):
            name = (prefix + "." if prefix else "") + _toml_key(key)
            inner = toml_dumps(value, name)
            out += ("\n" if out else "") + (inner or "[" + name + "]\n")
    return out


def shared_codex_config(text: str) -> str:
    """Codex config.toml reduced to extensions: no provider, login, project-trust or other routing/auth settings."""
    import tomllib
    data = tomllib.loads(text)
    kept = {key: data[key] for key in SHARE_TOML_KEYS if key in data}
    kept.update({key: data[key] for key in SHARE_TOML_TABLES if isinstance(data.get(key), dict)})
    return toml_dumps(kept)


def _write_private(path: Path, data: bytes) -> None:
    if not path.parent.is_dir():
        private_dir(path.parent)   # new folders get the private ACL; existing ones are already protected
    assert_private_path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            protect_path(temporary)
            stream.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _merge_shared_settings(current: dict, shared: dict, previous: dict, follow_source: bool = False) -> dict:
    """Only update values still equal to our last copy; keep personal edits and deletions."""
    managed = dict(previous)
    if follow_source:
        for key in previous.keys() - shared.keys():
            if current.get(key) == previous[key]:
                current.pop(key, None)
                managed.pop(key, None)
    for key in shared:
        if key in previous:
            if key not in current or current[key] != previous[key]:
                continue
        elif key in current:
            continue
        current[key] = shared[key]
        managed[key] = shared[key]
    return managed


def _retain_missing(current: dict, incoming: dict) -> dict:
    """Source removal is not permission to remove previously imported settings."""
    result = dict(current)
    for key, value in incoming.items():
        result[key] = (_retain_missing(result[key], value)
                       if isinstance(result.get(key), dict) and isinstance(value, dict) else value)
    return result


def sync_extensions(account: dict, root: Path, home: Path | None = None, *, follow_source: bool = False) -> dict:
    """Explicitly copy safe extensions; preserve personal edits and content absent from the source.

    ``follow_source`` is the automatic refresh at launch: a file that cannot be copied safely is skipped (and counted)
    instead of keeping the CLI from opening. Links in the profile are never read, written or removed through."""
    tool = account["tool"]
    summary = {"copied": 0, "removed": 0, "settings": [], "skipped": 0, "linked": [], "limited": False}
    source = user_config_dir(tool, home)
    target = profile_dir(root, account)
    assert_private_path(source)
    if os.path.abspath(source) == os.path.abspath(target):
        return summary
    private_dir(target)
    managed = set(SHARE_DIRS[tool]) | set(SHARE_FILES[tool]) | {"settings.json", "config.toml"}
    linked = sorted(entry.name for entry in os.scandir(target)
                    if (entry.name in managed or entry.name.lower().endswith(".md")) and not _plain(Path(entry.path)))
    summary["linked"] = linked
    manifest_path = target / SHARE_MANIFEST
    try:
        manifest = json.loads(read_private(manifest_path)) if manifest_path.is_file() else {}
    except (ValueError, OSError):
        manifest = {}
    old_files = set(manifest.get("files", []))
    hashes = dict(manifest.get("file_hashes", {}))
    if not source.is_dir() and not follow_source:
        return summary
    files = _shareable_files(source, tool, follow_skill_links=follow_source)
    if len(files) > SHARE_MAX_FILES:
        if not follow_source:
            raise ValueError("Too many extension files to share; trim " + str(source))
        files, summary["limited"] = files[:SHARE_MAX_FILES], True
    # (size, mtime) of source and copy when they were last compared: a launch skips reading files neither side changed.
    known_stats = manifest.get("file_stats", {}) if follow_source and isinstance(manifest.get("file_stats"), dict) else {}
    file_stats = {}
    wanted = set()
    for relative in files:
        name = relative.as_posix()
        try:      # only skills may be links (followed on purpose); everything else was checked to be a plain file
            origin = (source / relative).resolve() if follow_source and relative.parts[0] == "skills" else source / relative
        except (OSError, RuntimeError):
            origin = None
        if origin is not None and any(part.lower() in NEVER_COPY for part in origin.parts):
            continue  # A skill symlink must not disguise an authentication file as a Markdown resource.
        wanted.add(name)      # before any failure: a file that cannot be read now is not "removed from the source"
        if relative.parts[0] in linked:
            continue
        destination = target / relative
        try:
            if origin is None:
                raise ValueError("Cannot resolve the source extension")
            seen = os.stat(origin)
            try:
                kept = destination.stat()
                mark = [seen.st_size, seen.st_mtime_ns, kept.st_size, kept.st_mtime_ns]
            except OSError:
                mark = None
            if mark is not None and name in hashes and known_stats.get(name) == mark:
                file_stats[name] = mark
                continue
            data = read_private_bytes(origin)
            if len(data) > SHARE_MAX_FILE:
                raise ValueError("An extension grew beyond the import size limit")
            digest = hashlib.sha256(data).hexdigest()
            try:
                if destination.is_file():
                    current = read_private_bytes(destination)
                    if current == data:
                        hashes[name] = digest
                    if current == data or hashlib.sha256(current).hexdigest() != hashes.get(name):
                        if mark is not None:
                            file_stats[name] = mark      # same answer next time unless one side changes
                        continue
                elif name in old_files:
                    continue  # A personally deleted extension stays deleted.
            except (OSError, ValueError):
                raise ValueError("Cannot safely read the destination extension") from None
            _write_private(destination, data)
            hashes[name] = digest
            kept = destination.stat()
            file_stats[name] = [seen.st_size, seen.st_mtime_ns, kept.st_size, kept.st_mtime_ns]
            summary["copied"] += 1
        except (OSError, ValueError):
            if not follow_source:
                raise
            summary["skipped"] += 1
    if follow_source and not summary["limited"]:   # a truncated list cannot tell what the source removed
        for name in sorted(old_files - wanted):
            relative = Path(name)
            allowed = (relative.parts and (relative.parts[0] in SHARE_DIRS[tool]
                       or (len(relative.parts) == 1 and (relative.suffix.lower() == ".md" or name in SHARE_FILES[tool]))))
            if (not allowed or relative.is_absolute() or ".." in relative.parts or "\\" in name
                    or any(part.lower() in NEVER_COPY for part in relative.parts) or name not in hashes
                    or relative.parts[0] in linked):
                continue
            destination = target / relative
            try:
                assert_private_path(destination)
                if destination.is_file() and hashlib.sha256(read_private_bytes(destination)).hexdigest() == hashes[name]:
                    destination.unlink()
                    hashes.pop(name, None)
                    old_files.discard(name)
                    summary["removed"] += 1
            except (OSError, ValueError):
                summary["skipped"] += 1
    settings_snapshot = manifest.get("settings_snapshot", {})
    env_snapshot = manifest.get("env_snapshot", {})
    try:
        if tool == "claude" and "settings.json" not in linked:
            source_settings = source / "settings.json"
            try:
                user = json.loads(read_user_text(source_settings)) if source_settings.is_file() else {}
                if not isinstance(user, dict):
                    raise ValueError("Expected a settings object")
            except (OSError, ValueError):
                raise ValueError("Cannot safely import the source Claude settings; repair settings.json") from None
            shared, env = shared_settings(user)
            settings_path = target / "settings.json"
            try:
                current = json.loads(read_private(settings_path)) if settings_path.is_file() else {}
                if not isinstance(current, dict):
                    raise ValueError
            except OSError:
                current = {}
            managed_settings = _merge_shared_settings(current, shared, settings_snapshot, follow_source)
            merged_env = dict(current["env"]) if isinstance(current.get("env"), dict) else {}
            managed_env = _merge_shared_settings(merged_env, env, env_snapshot, follow_source)
            if merged_env:
                current["env"] = merged_env
            else:
                current.pop("env", None)
            _write_private(settings_path, json.dumps(current, ensure_ascii=False, indent=2).encode("utf-8"))
            settings_snapshot, env_snapshot = managed_settings, managed_env   # only what was really written
            summary["settings"] = sorted(shared)
        elif tool == "codex" and "config.toml" not in linked:
            config_source = source / "config.toml"
            try:
                reduced = shared_codex_config(read_user_text(config_source)) if config_source.is_file() else None
            except ImportError:
                reduced = None                     # Python < 3.11 on a remote host: skip the config, keep the folders
                summary["warning"] = "This host needs Python 3.11 to import Codex config.toml"
            except ValueError:
                raise ValueError("Cannot safely import the source Codex settings; repair config.toml") from None
            config_path = target / "config.toml"
            previous_hash = manifest.get("config_hash")
            current_data = read_private_bytes(config_path) if config_path.is_file() else None
            # Preserve edited TOML verbatim, including comments and settings outside the shared whitelist.
            unchanged = current_data is not None and hashlib.sha256(current_data).hexdigest() == previous_hash
            if reduced is not None and (unchanged or (current_data is None and previous_hash is None)):
                if current_data is not None and not follow_source:
                    import tomllib
                    reduced = toml_dumps(_retain_missing(tomllib.loads(current_data.decode("utf-8-sig")),
                                                        tomllib.loads(reduced)))
                data = reduced.encode("utf-8")
                _write_private(config_path, data)
                manifest["config_hash"] = hashlib.sha256(data).hexdigest()
                summary["settings"] = ["config.toml"]
            elif follow_source and unchanged and not config_source.exists():
                config_path.unlink()
                manifest.pop("config_hash", None)
    except (OSError, ValueError, TypeError):
        if not follow_source:
            raise
        summary["skipped"] += 1
    manifest.update({"files": sorted(old_files | wanted), "file_hashes": hashes, "file_stats": file_stats,
                     "settings": list(settings_snapshot), "env": list(env_snapshot),
                     "settings_snapshot": settings_snapshot, "env_snapshot": env_snapshot})
    _write_private(manifest_path, json.dumps(manifest).encode("utf-8"))
    return summary


def read_user_text(path: Path, limit: int = 1048576) -> str:
    """A file the user owns outside launcher data (settings.json, config.toml, plugin registry).

    Links and ordinary permissions (0644) are fine here: callers only take whitelisted values from it."""
    with os.fdopen(os.open(path, os.O_RDONLY), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_uid != os.getuid()):
            raise ValueError("Unsupported user file: " + str(path))
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("User file exceeds size limit: " + str(path))
    return data.decode("utf-8-sig")


def read_private_bytes(path: Path, limit: int = SHARE_MAX_FILE) -> bytes:
    assert_private_path(path)
    with path.open("rb") as stream:
        return stream.read(limit + 1)


def user_plugin_dirs(account: dict, home: Path | None = None, *, project: Path | None = None) -> list[str]:
    """Claude plugins the user installed and enabled, passed with --plugin-dir (their paths are absolute, so copying breaks them)."""
    if account.get("tool") != "claude":
        return []
    base = user_config_dir("claude", home)
    plugins = base / "plugins"
    try:
        installed = json.loads(read_user_text(plugins / "installed_plugins.json")).get("plugins", {})
        settings = json.loads(read_user_text(base / "settings.json"))
        enabled = dict(settings.get("enabledPlugins", {})) if isinstance(settings, dict) else {}
        if project is not None:
            # Only the project folder itself: a parent such as C:\ or /tmp may hold a .claude folder someone else made.
            for filename in ("settings.json", "settings.local.json"):
                path = project / ".claude" / filename
                if path.is_file():
                    local = json.loads(read_user_text(path)).get("enabledPlugins", {})
                    if isinstance(local, dict):
                        enabled.update(local)
    except (OSError, ValueError, AttributeError, TypeError):
        return []
    found = []
    for name, entries in installed.items() if isinstance(installed, dict) else []:
        if enabled.get(name) is False or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path = Path(str(entry.get("installPath", "")))
            scope = entry.get("scope", "user")
            in_project = project is not None and _same_project_path(entry.get("projectPath"), str(project))
            if (scope == "user" or (scope in {"project", "local"} and in_project)) and path.is_dir() and _plain(path) and str(path) not in found:
                try:
                    path.resolve().relative_to(plugins.resolve())
                except ValueError:
                    continue
                found.append(str(path))
    return found


def manual_import(account: dict, root: Path, kind: str, home: Path | None = None) -> dict:
    """The Accounts page's import button: refresh settings or conversations right now."""
    if account.get("tool") not in {"codex", "claude"} or kind not in {"settings", "history"}:
        raise ValueError("Unknown import kind or tool")
    summary = sync_extensions(account, root, home) if kind == "settings" else sync_history(account, root, home)
    return {"status": "imported", "kind": kind, "summary": summary}


def account_fits(account: dict, environment_id: str) -> bool:
    """A subscription login lives in ONE place (the machine it was made on), so it is only usable there.

    An API key / endpoint account has no such place: the key is handed to whichever environment starts the CLI.
    """
    if account.get("auth") != "subscription":
        return True
    bound = account.get("environment_id") or ""
    return not bound or bound == (environment_id or "local")


# --- project folders that do not exist yet ---------------------------------------------------------------------
def folder_plan(path: Path) -> tuple[Path | None, list[Path]]:
    """(deepest folder that already exists, the folders that would have to be created, outermost first)."""
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            return None, list(reversed(missing))
        current = current.parent
    if not current.is_dir():
        raise ValueError(f"{current} is a file, so the project folder cannot be created under it")
    return current, list(reversed(missing))


def ensure_project_dir(path: Path, ask=None) -> Path:
    """Return an existing project folder, offering to create it (and its missing parents) first.

    ``ask(existing, missing)`` decides; the default asks in the terminal this job runs in, colouring the folders
    that will be created so they cannot be mistaken for the ones that already exist.
    """
    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError("Project must be an absolute directory (or ~/directory)")
    if path.is_dir():
        return path
    existing, missing = folder_plan(path)
    if not (ask or terminal_confirm_create)(existing, missing):
        raise ValueError("The project folder does not exist and was not created")
    path.mkdir(parents=True, exist_ok=True)
    return path


def terminal_confirm_create(existing: Path | None, missing: list[Path]) -> bool:
    if not sys.stdin.isatty():
        raise ValueError(f"Project folder does not exist: {missing[-1]}")
    dim, green, bold, reset = "\x1b[2m", "\x1b[1;32m", "\x1b[1m", "\x1b[0m"
    print(tr('\n{v0}Project folder does not exist{v1}：{v2}', v0=f'{bold}', v1=f'{reset}', v2=f'{missing[-1]}'))
    if existing is not None:
        print(tr('  {v0}\u3000 {v1}    Already exists{v2}', v0=f'{dim}', v1=f'{existing}', v2=f'{reset}'))
    for folder in missing:
        print(tr('  {v0}＋ {v1}    Will be created{v2}', v0=f'{green}', v1=f'{folder}', v2=f'{reset}'))
    noun = tr('These {v0} folders', v0=f'{len(missing)}') if len(missing) > 1 else tr('This folder')
    try:
        answer = input(tr('Create {v0}? [y/N] ', v0=f'{noun}'))
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes", "是"}


def mark_onboarded(account: dict, root: Path) -> None:
    """A Claude profile that is already signed in should not start with the first-run welcome wizard again."""
    if account.get("tool") != "claude" or account.get("auth") != "subscription":
        return
    path = profile_dir(root, account) / ".claude.json"
    try:
        data = json.loads(read_private(path))
    except (OSError, ValueError):
        return
    if isinstance(data, dict) and data.get("oauthAccount") and not data.get("hasCompletedOnboarding"):
        data["hasCompletedOnboarding"] = True
        write_json(path, data)


CLAUDE_SEEN_VERSIONS = ("lastOnboardingVersion", "lastReleaseNotesSeen", "lastClawdEntranceVersion")


def carry_claude_onboarding(source: dict, target: dict, root: Path, project: str) -> None:
    """A handoff opens the conversation on ``target`` where ``source`` already worked: the same person finished the
    first-run wizard, saw the one-time notices and trusted this folder, so the target shows none of them again (a
    notice would take the first keys typed, the continuation included). Nothing secret is copied."""
    mark_onboarded(target, root)
    path = profile_dir(root, target) / ".claude.json"
    try:
        known = json.loads(read_private(profile_dir(root, source) / ".claude.json", limit=16 * 1048576))
        data = json.loads(read_private(path, limit=16 * 1048576))
    except (OSError, ValueError):
        return
    if not isinstance(known, dict) or not isinstance(data, dict):
        return
    seen = {name: value for name, value in known.items() if name not in data and (
        (name.startswith(("hasSeen", "hasAcknowledged")) and value is True)
        or (name in CLAUDE_SEEN_VERSIONS and isinstance(value, str) and len(value) <= 64))}
    data.update(seen)
    changed = bool(seen)
    key = str(project).replace("\\", "/").casefold()

    def trusted(projects) -> list[str]:
        return [name for name, value in (projects.items() if isinstance(projects, dict) else ())
                if isinstance(value, dict) and value.get("hasTrustDialogAccepted") is True
                and name.replace("\\", "/").casefold() == key]
    source_trust = trusted(known.get("projects"))
    if source_trust and not trusted(data.get("projects")):
        projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
        entry = projects.get(source_trust[0]) if isinstance(projects.get(source_trust[0]), dict) else {}
        projects[source_trust[0]] = dict(entry, hasTrustDialogAccepted=True)
        data["projects"], changed = projects, True
    if changed:
        write_json(path, data)


def _copy_transcripts(tool: str, source: Path, base: Path, target: Path, summary: dict, budget: dict,
                      want_path=None, want_record=None, refresh: bool = False) -> None:
    """Copy transcripts under ``base`` (inside ``source``) into the same relative place under ``target``.

    Existing files are never replaced. ``want_path(path)`` / ``want_record(first_record)`` narrow what is imported (for
    example to one project); ``budget`` (visited, copied_bytes) is shared by every call of one import."""
    pending = [base]
    while pending:
        directory = pending.pop()
        assert_private_path(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                budget["visited"] += 1
                if budget["visited"] > 10000 or budget["copied"] >= 256 * 1024 * 1024:
                    summary["limited"] = True
                    return
                path = Path(entry.path)
                temporary = None
                try:
                    assert_private_path(path)
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                        continue
                    if not entry.is_file(follow_symlinks=False) or path.suffix != ".jsonl":
                        continue
                    if tool == "codex" and not path.name.startswith("rollout-"):
                        continue
                    destination = target / path.relative_to(source)
                    assert_private_path(destination)
                    if destination.exists():
                        if not refresh:
                            continue
                        kept, seen = destination.stat(), entry.stat(follow_symlinks=False)
                        if kept.st_size > seen.st_size or (kept.st_size, kept.st_mtime_ns) == (seen.st_size, seen.st_mtime_ns):
                            continue  # unchanged (copies keep the source mtime) or continued here: nothing to add
                    if want_path is not None and not want_path(path):
                        continue
                    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    with os.fdopen(os.open(path, flags), "rb") as stream:
                        before = os.fstat(stream.fileno())
                        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                                or before.st_size > 64 * 1024 * 1024
                                or before.st_size > 256 * 1024 * 1024 - budget["copied"]):
                            summary["skipped"] += 1
                            continue
                        data = stream.read(64 * 1024 * 1024 + 1)
                        after = os.fstat(stream.fileno())
                    if (len(data) != before.st_size or before.st_mtime_ns != after.st_mtime_ns
                            or after.st_size != before.st_size or not data.endswith(b"\n")):
                        summary["skipped"] += 1
                        continue
                    first = None
                    for line in data.splitlines():
                        record = json.loads(line)
                        if not isinstance(record, dict):
                            raise ValueError("Invalid transcript record")
                        if first is None:
                            first = record
                    if want_record is not None and not want_record(first):
                        continue
                    if tool == "codex":
                        metadata = (first or {}).get("payload")
                        if ((first or {}).get("type") != "session_meta" or not isinstance(metadata, dict)
                                or metadata.get("history_mode", "legacy") != "legacy"):
                            # Paginated Codex sessions store turns in SQLite, not this transcript.
                            summary["skipped"] += 1
                            continue
                    previous = None
                    if destination.exists():
                        previous = read_private_bytes(destination, limit=64 * 1024 * 1024)
                        if not previous or not previous.endswith(b"\n") or previous == data or not data.startswith(previous):
                            continue  # A locally continued or edited conversation must never be replaced.
                    private_dir(destination.parent)
                    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
                    assert_private_path(temporary)
                    with temporary.open("xb") as stream:
                        protect_path(temporary)
                        stream.write(data)
                    os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
                    assert_private_path(destination)
                    if previous is not None:
                        if read_private_bytes(destination, limit=64 * 1024 * 1024) != previous:
                            summary["skipped"] += 1
                            continue
                        os.replace(temporary, destination)
                    elif os.name == "nt":
                        os.rename(temporary, destination)  # Windows rename refuses an existing destination.
                    else:
                        os.link(temporary, destination)    # Atomic no-replace publication on the SSH host.
                        temporary.unlink()
                    temporary = None
                    summary["copied"] += 1
                    budget["copied"] += len(data)
                except (OSError, ValueError):
                    summary["skipped"] += 1
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)


def sync_history(account: dict, root: Path, home: Path | None = None, *, refresh: bool = False) -> dict:
    """Import absent transcript snapshots; never merge credentials, indexes, or live profile files."""
    summary = {"copied": 0, "skipped": 0, "limited": False}
    tool = account["tool"]
    source = user_config_dir(tool, home)
    target = profile_dir(root, account)
    folder = "sessions" if tool == "codex" else "projects"
    base = source / folder
    assert_private_path(base)
    assert_private_path(target)
    if not base.is_dir() or os.path.abspath(source) == os.path.abspath(target):
        return summary
    # ponytail: bounded snapshots; large archives need a dedicated import flow.
    _copy_transcripts(tool, source, base, target, summary, {"visited": 0, "copied": 0}, refresh=refresh)
    return summary


def _same_project_path(left: object, right: str) -> bool:
    return isinstance(left, str) and bool(left) and os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def _codex_rollout_cwd(path: Path) -> object:
    """The working directory a Codex rollout was recorded in (its first line only; a bad line means "not this one")."""
    try:
        with path.open("rb") as stream:
            first = json.loads(stream.readline(1024 * 1024))
        return first["payload"].get("cwd") if first.get("type") == "session_meta" else None
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return None


def claude_project_folder(project_path: str) -> str:
    """The folder name Claude Code files a project's conversations under: every non-alphanumeric character becomes ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", project_path)


def sync_project_history(account: dict, root: Path, project_path: str, home: Path | None = None) -> dict:
    """Make this account's profile see the conversations other accounts of the same tool had in ``project_path``.

    Sources include the user's native CLI home and other launcher profiles in this environment.
    Only absent transcripts of that project are copied; credentials and indexes are never touched."""
    summary = {"copied": 0, "skipped": 0, "limited": False, "sources": 0}
    tool = account.get("tool")
    if tool not in {"codex", "claude"} or not project_path:
        return summary
    target = profile_dir(root, account)
    assert_private_path(target)
    parent = target.parent
    folder = "sessions" if tool == "codex" else "projects"
    if tool == "codex":
        def want_path(path: Path) -> bool:
            return _same_project_path(_codex_rollout_cwd(path), project_path)
    else:
        expected = claude_project_folder(project_path)

        def want_path(path: Path) -> bool:
            return expected in path.parts[:-1]
    budget = {"visited": 0, "copied": 0}
    sources = [user_config_dir(tool, home)] + (sorted(parent.iterdir()) if parent.is_dir() else [])
    for sibling in sources:
        if sibling == target or sibling.name == target.name:
            continue
        base = sibling / folder
        try:
            assert_private_path(sibling)
            assert_private_path(base)
            if not sibling.is_dir() or not base.is_dir():
                continue
            summary["sources"] += 1
            _copy_transcripts(tool, sibling, base, target, summary, budget, want_path=want_path)
        except (OSError, ValueError):
            summary["skipped"] += 1
        if summary["limited"]:
            break
    return summary


def sync_user_data(account: dict, root: Path, project_path: str, home: Path | None = None, *, session_id: str = "") -> dict:
    """Refresh native user data on the machine running the CLI, while keeping credentials isolated.

    Every stage is best effort: a stage that fails is named in ``failed`` and never keeps the CLI from opening."""
    source = account
    result = {"extensions": {}, "history": {"copied": 0, "skipped": 0, "limited": False}, "failed": []}
    try:
        result["extensions"] = sync_extensions(source, root, home, follow_source=True)
    except Exception:
        result["failed"].append("extensions")
    try:
        refresh = True
        for path in (root / "sessions").glob("*.json"):
            try:
                record = json.loads(read_private(path))
                same_account = record.get("account", {}).get("id") == account["id"]
                if same_account and record.get("session_id") != session_id and record.get("status") not in {"ended", "failed"}:
                    refresh = False
                    break  # Another CLI may be appending to a profile transcript; only add absent files then.
            except (OSError, ValueError, AttributeError):
                refresh = False
                break
        if not (account["tool"] == "codex" and native_codex_state(home)):
            result["history"] = sync_history(source, root, home, refresh=refresh)
    except Exception:
        result["failed"].append("history")
    if account.get("share_project_history") is True:
        try:
            sync_project_history(account, root, project_path, home)
        except Exception:
            result["failed"].append("project history")
    return result


def prepare(account: dict, root: Path) -> dict:
    directory = profile_dir(root, account)
    private_dir(directory)
    env = isolated_env(account, root)
    if account["auth"] == "api":
        key = api_key(account, root)
        env["VIBE_API_KEY" if account["tool"] == "codex" else "ANTHROPIC_API_KEY"] = key
    else:
        env.pop("VIBE_API_KEY", None)
    return env


def check_login(account: dict, root: Path, env: dict) -> bool:
    result = subprocess.run(cli_command(account, "status", root), env=env, cwd=profile_dir(root, account),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if account["tool"] == "claude":
        try:
            return result.returncode == 0 and json.loads(result.stdout).get("loggedIn") is True
        except (ValueError, AttributeError):
            return False
    return result.returncode == 0


def authenticate(request: dict, root: Path, force: bool = False) -> dict:
    account = request["account"]
    private_dir(profile_dir(root, account))
    check_settings(account, root, profile_dir(root, account))
    if account["auth"] == "api":
        if force:
            api_key(account, root, replace=True)
        env = prepare(account, root)
        emit(request, {"auth_status": "configured"})
        return env
    env = prepare(account, root)
    if force or not check_login(account, root, env):
        emit(request, {"status": "waiting_auth", "auth_status": "waiting_auth"})
        result = subprocess.call(cli_command(account, "login", root, request.get("login_method", "device")),
                                 env=env, cwd=profile_dir(root, account))
        if result != 0 or not check_login(account, root, env):
            emit(request, {"auth_status": "unknown"})
            raise RuntimeError("Login incomplete. Retry login; Codex also supports browser callback mode.")
    if request.get("action") == "login":
        print("\nLogin verified for this profile on this machine. You can now start projects with this account here.", flush=True)
    emit(request, {"auth_status": "authenticated"})
    return env


def session_path(root: Path, session_id: str) -> Path:
    return root / "sessions" / (safe_id(session_id) + ".json")


def save_status(root: Path, request: dict, status: str, **extra) -> None:
    record = dict(request)
    record.update(status=status, updated_at=now(), **extra)
    write_json(session_path(root, request["session_id"]), record)
    emit(request, {"session_id": request["session_id"], "status": status, **extra})


def tmux(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    # -u: the terminal is UTF-8. ssh forwards no LANG, and without it tmux replaces every non-ASCII character with "_".
    # The C locale is kept only for output this module reads.
    return subprocess.run([trusted_executable("tmux"), "-u", "-L", "vibe-launcher", *args],
                          env={**os.environ, "LC_ALL": "C"} if capture else dict(os.environ),
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def utf8_locale(env: Mapping[str, str]) -> list[str]:
    """``env`` arguments that give a tmux worker a UTF-8 locale: a server started by an older helper carries
    LC_ALL=C, and a non-interactive SSH command often has no locale at all."""
    result = [] if env.get("LC_ALL") else ["-u", "LC_ALL"]
    if not any("utf8" in env.get(name, "").lower().replace("-", "") for name in ("LC_ALL", "LC_CTYPE", "LANG")):
        result.append("LC_CTYPE=C.UTF-8")
    return result


# Terminals on a server live in tmux: one tmux session per launcher workspace (named after the workspace id), one
# window per terminal, marked with its session id. A terminal tab shows its window through a short-lived "view"
# session that holds only that window and disappears when the tab leaves; the work stays in the workspace session.
def tmux_name(session_id: str) -> str:
    return "vibe-" + safe_id(session_id)


def tmux_group(workspace_id: str) -> str:
    return "vibe-ws-" + safe_id(workspace_id or "default")


def tmux_windows() -> dict[str, str]:
    """Terminal session id -> tmux window id (``@n``), read from the windows' ``@vibe-session`` marks."""
    result = tmux("list-windows", "-a", "-F", "#{window_id} #{@vibe-session}", capture=True)
    if result.returncode:
        error = (result.stderr or b"").strip()
        if error.startswith(b"no server running on ") or (
                error.startswith(b"error connecting to ") and error.endswith(b"(No such file or directory)")):
            return {}
        raise RuntimeError("Cannot verify tmux session state; check socket access before retrying")
    found: dict[str, str] = {}
    for line in (result.stdout or b"").decode("utf-8", "replace").splitlines():
        window, _, session_id = line.partition(" ")
        if session_id:
            found.setdefault(session_id, window)
    return found


def tmux_session_exists(session_id: str) -> bool:
    return session_id in tmux_windows()


def end_workspace_tmux(workspace_id: str) -> int:
    """A deleted workspace: end its tmux session, including windows whose records were lost."""
    result = tmux("kill-session", "-t", "=" + tmux_group(workspace_id), capture=True)
    error = (result.stderr or b"").strip()
    gone = (error.startswith(b"can't find session") or error.startswith(b"no server running on ")
            or (error.startswith(b"error connecting to ") and error.endswith(b"(No such file or directory)")))
    return 0 if result.returncode == 0 or gone else result.returncode


def tmux_label(request: dict) -> str:
    """The window's name in tmux's status line: project folder and tool."""
    folder = Path(request.get("path") or "").name or "project"
    tool = "terminal" if is_shell(request) else request.get("account", {}).get("tool", "agent")
    return "".join(c for c in f"{folder} · {tool}" if c.isprintable() and c not in "#;")[:48]


def tmux_new_window(group: str, label: str, path: str, command: str) -> str | None:
    """Start ``command`` as a new window of the workspace session (created when missing); returns the window id."""
    options = ("-d", "-P", "-F", "#{window_id}", "-n", label, "-c", path)
    # Two terminals of one workspace may start together: whoever loses the race to create the session adds a window.
    for kind in ("window", "session", "window"):
        if kind == "window":
            result = tmux("new-window", "-t", "=" + group + ":", *options, command, capture=True)
        else:
            result = tmux("new-session", "-s", group, *options, command, capture=True)
        window = (result.stdout or b"").decode("ascii", "replace").strip()
        if result.returncode == 0 and re.fullmatch(r"@\d+", window):
            return window
    return None


def tmux_version() -> tuple[int, int]:
    result = tmux("-V", capture=True)
    match = re.search(rb"(\d+)\.(\d+)", result.stdout or b"")
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


# tmux before 3.6 (3.4 seen) hands the terminal's answer to its own attach-time device-attributes query to the window as
# typed text ("61;6;7;...c" at a shell prompt). Attached on a placeholder for this long, the answer lands there instead.
ABSORB_SECONDS = 1


def attach_terminal(request: dict) -> int | None:
    """Show one terminal's window in this SSH terminal; None when it is not running."""
    window = tmux_windows().get(request["session_id"])
    if window is None:
        return None
    view = tmux_name(request["session_id"]) + "-" + safe_id(request.get("nonce") or os.urandom(4).hex())[:8]
    exact = "=" + view + ":"
    absorb = tmux_version() < (3, 6)
    # A session cannot start empty: open it on a placeholder, link the terminal's window, drop the placeholder.
    created = tmux("new-session", "-d", "-P", "-F", "#{window_id}", "-s", view, "sleep 60", capture=True)
    placeholder = (created.stdout or b"").decode("ascii", "replace").strip()
    if created.returncode or not re.fullmatch(r"@\d+", placeholder):
        raise RuntimeError("Cannot open the tmux terminal")
    linked = tmux("link-window", "-a", *(["-d"] if absorb else []), "-s", window, "-t", exact, capture=True)
    if not absorb:
        tmux("kill-window", "-t", placeholder, capture=True)
    if linked.returncode:
        tmux("kill-session", "-t", "=" + view, capture=True)
        return None
    # Options take a pane target: an exact session match is "=name:" there ("=name" alone is "no such session").
    tmux("set-option", "-t", exact, "prefix2", "C-b", capture=True)  # best effort: lets the app send "detach"
    # tmux then publishes "vibe:<session>" as the terminal title, which is how the app knows it is really attached.
    tmux("set-option", "-t", exact, "set-titles", "on", capture=True)
    tmux("set-option", "-t", exact, "set-titles-string", "vibe:#S", capture=True)
    # Codex and Claude follow focus changes; this server is the launcher's own (-L), so the user's tmux is untouched.
    tmux("set-option", "-s", "focus-events", "on", capture=True)
    # The view goes when the tab leaves. Set before the client is attached, tmux would destroy it at once.
    commands = ["attach-session", "-t", "=" + view, ";", "set-option", "-t", exact, "destroy-unattached", "on"]
    if absorb:
        drop = shlex.join([trusted_executable("tmux"), "-L", "vibe-launcher", "kill-window", "-t", placeholder])
        commands += [";", "run-shell", "-b", f"sleep {ABSORB_SECONDS}; {drop}"]
    return tmux(*commands).returncode


def version_number(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?<![\w.])(\d+)\.(\d+)\.(\d+)(?![\w.-])", value)
    return tuple(map(int, match.groups())) if match else None


def maintenance_env(tool: str, root: Path) -> dict:
    """Environment for looking at / updating the CLI itself (as opposed to running an account with it).

    Codex finds out how it was installed through CODEX_HOME (the standalone install lives in ~/.codex/packages), so the
    updater must see the real one; pointing it at an empty launcher folder makes it answer "could not detect the
    installation method". Claude gets a private (existing) folder of its own.
    """
    env = isolated_env({"id": "maintenance", "tool": tool, "auth": "subscription"}, root)
    if tool == "codex":
        env.pop("CODEX_HOME", None)
        if os.environ.get("CODEX_HOME"):
            env["CODEX_HOME"] = os.environ["CODEX_HOME"]
    else:
        private_dir(profile_dir(root, {"id": "maintenance", "tool": tool}))
    return env


def tool_output(args: list[str], root: Path, tool: str) -> str:
    env = maintenance_env(tool, root)
    env["LC_ALL"] = "C"
    result = subprocess.run(args, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
    if result.returncode:
        raise RuntimeError("Tool inspection failed: " + Path(args[0]).name)
    return result.stdout.decode("utf-8", errors="replace")


def upstream_version(tool: str) -> str:
    package = "@openai%2Fcodex" if tool == "codex" else "@anthropic-ai%2Fclaude-code"
    request = Request("https://registry.npmjs.org/" + package + "/latest", headers={"User-Agent": "VibeLauncher"})
    with urlopen(request, timeout=15) as response:
        data = response.read(1048577)
    if len(data) > 1048576:
        raise ValueError("Version response too large")
    value = json.loads(data)["version"]
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Invalid release version")
    return value


def update_route(tool: str, path: str, root: Path, channel: str = "") -> tuple[str, list[str], dict]:
    executable = Path(path)
    if os.name != "nt" and str(executable.parent) in {"/snap/bin", "/var/lib/snapd/snap/bin"}:
        snap = trusted_executable("snap")
        info = tool_output([snap, "info", tool], root, tool)
        def field(name):
            match = re.search(r"^" + name + r":\s*(.+)$", info, re.M)
            return match.group(1).strip() if match else "unknown"
        tracking = field("tracking")
        chosen = channel or tracking
        release = re.search(r"^\s+" + re.escape(chosen) + r":\s+(\S+)", info, re.M)
        candidate = release.group(1) if release and version_number(release.group(1)) else "unknown"
        command = [snap, "refresh", tool] + (["--channel=" + channel] if channel else [])
        if os.geteuid() != 0:
            command.insert(0, trusted_executable("sudo"))
        return "Snap", command, {"channel": chosen, "publisher": field("publisher"), "candidate": candidate}
    if channel:
        raise ValueError("Snap channel applies only to Snap installations")
    package = "@openai/codex" if tool == "codex" else "@anthropic-ai/claude-code"
    # Only update npm when this very executable belongs to the selected package.
    for directory in executable.resolve().parents:
        manifest = directory / "package.json"
        if manifest.is_file():
            try:
                name = json.loads(manifest.read_text(encoding="utf-8"))["name"]
            except (ValueError, KeyError):
                continue
            if name == package:
                npm = trusted_executable("npm")
                npm_root = tool_output([npm, "root", "-g"], root, tool).strip()
                if (Path(npm_root) / package).resolve() != directory:
                    raise RuntimeError("npm installation differs from the executable selected by PATH")
                return "npm", [npm, "install", "-g", package + "@latest"], {}
    help_text = tool_output([path, "--help"], root, tool)
    if re.search(r"^\s+update(?:\s|\|)", help_text, re.M):
        return "CLI updater", [path, "update"], {}
    return "manual", [], {}


def inspect_tool(tool: str, root: Path, channel: str = "") -> dict:
    report = {"tool": tool, "checked_at": now(), "path": "", "installed": "unknown", "latest": "unknown",
              "source": "unknown", "status": "unknown", "message": ""}
    try:
        path = trusted_executable(tool)
        report["path"] = path
        version = tool_output([path, "--version"], root, tool).strip()
        number = version_number(version)
        if not number:
            raise RuntimeError("Cannot determine installed CLI version")
        report["installed"] = ".".join(map(str, number))
        source, command, details = update_route(tool, path, root, channel)
        report.update(source=source, can_update=bool(command), **details)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report["message"] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
    try:
        report["latest"] = upstream_version(tool)
        current, latest = version_number(report["installed"]), version_number(report["latest"])
        if current and latest:
            report["status"] = "update_available" if current < latest else ("current" if current == latest else "newer_than_registry")
    except Exception:
        report["message"] += " Latest release lookup unavailable; retry later."
    return report


def manage_tools(request: dict, root: Path) -> int:
    reports = []
    code = 0
    for tool in ([request["tool"]] if request["action"] == "update-tool" else ["codex", "claude"]):
        report = inspect_tool(tool, root, request.get("snap_channel", ""))
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if request["action"] == "update-tool":
            try:
                if not report.get("can_update"):
                    raise RuntimeError("Cannot safely identify an updater. Use the original installer, then check again.")
                source, command, _ = update_route(tool, report["path"], root, request.get("snap_channel", ""))
                print("Updater: " + shlex.join(command), flush=True)
                print("Existing sessions keep their running version. New sessions resolve PATH again.", flush=True)
                if source == "Snap":
                    print("Snap publisher: " + report.get("publisher", "unknown") + "; channel: " + report.get("channel", "unknown"), flush=True)
                    if input("Update this Snap package/channel? Type yes to proceed: ").strip() != "yes":
                        raise RuntimeError("Update cancelled")
                env = maintenance_env(tool, root)
                result = subprocess.call(command, cwd=root, env=env)
                if result:
                    raise RuntimeError("Updater failed or was cancelled; version not confirmed")
                after = inspect_tool(tool, root, request.get("snap_channel", ""))
                if after["installed"] == "unknown":
                    raise RuntimeError("Updater exited successfully but active executable could not be verified")
                after["previous"] = report["installed"]
                after["message"] = ("Active version verified. " if after["installed"] != report["installed"]
                                    else "Updater finished; active version unchanged. Check channel, permissions and duplicate installations. ") + after["message"]
                report = after
            except (OSError, ValueError, RuntimeError, EOFError, KeyboardInterrupt, subprocess.SubprocessError) as error:
                report["status"] = "update_failed"
                report["message"] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                code = 1
        reports.append(report)
    path = root / "tool-versions" / (safe_id(request["environment_id"]) + ".json")
    previous = json.loads(read_private(path)).get("reports", []) if path.exists() else []
    merged = {r["tool"]: r for r in previous}
    merged.update({r["tool"]: r for r in reports})
    payload = {"environment_id": request["environment_id"], "reports": reports, "checked_at": now()}
    write_json(path, {**payload, "reports": list(merged.values())})
    print("\nVIBE_TOOLS_" + request["nonce"] + ":" + json.dumps(payload), flush=True)
    return code


def doctor(request: dict, root: Path) -> None:
    """Check what this environment can run.

    For a launch/login the chosen account's CLI is required. A plain environment check only needs *one* of the
    CLIs (nobody has to install both). tmux is required on Linux because it keeps sessions alive across disconnects.
    """
    account_tool = request["account"]["tool"] if request.get("account") else None
    # A plain shell session needs no agent CLI at all; only tmux (when it should survive the terminal).
    tools = [] if is_shell(request) else ([account_tool] if account_tool else ["codex", "claude"])
    names = tools + ([] if WINDOWS else ["tmux"])
    # A launch keeps running after the terminal closes (needs tmux) unless the project opted out. A plain environment
    # check or an account login does not need tmux at all: it is only reported.
    wants_tmux = not WINDOWS and request.get("persistent", request.get("action") == "launch") is True
    required = ([account_tool] if account_tool else []) + (["tmux"] if wants_tmux else [])
    locations = {}
    for name in names:
        try:
            locations[name] = trusted_executable(name)
        except RuntimeError:
            locations[name] = None
    missing = [name for name in required if not locations[name]]
    if tools and not account_tool and not any(locations[tool] for tool in tools):
        missing.append("codex or claude (at least one)")
    print("Python: " + sys.version.split()[0])
    for name in names:
        optional = ("not installed (only needed to keep a project running after you close the terminal)"
                    if name == "tmux" else "not installed (optional)")
        print(name + ": " + (locations[name] or ("MISSING" if name in required else optional)))
    if missing:
        if "tmux" in missing:
            print("tmux keeps remote sessions alive after you disconnect. It is not installed automatically: sudo apt install tmux (Ubuntu/Debian).")
        if any(name.startswith(("codex", "claude")) for name in missing):
            print("Install the CLI under your home or /usr/local/bin; no PATH editing needed for ~/.local/bin, ~/.npm-global/bin or nvm.")
            print("Codex: https://developers.openai.com/codex/cli/ ; Claude: https://code.claude.com/docs/en/setup")
        raise RuntimeError("Missing required tools: " + ", ".join(missing))
    emit(request, {"status": "ready"})


def pid_alive(pid) -> bool | None:
    """True/False when a POSIX process can be checked; None when we cannot tell (Windows, missing or bad pid)."""
    if WINDOWS or os.name == "nt" or type(pid) is not int or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


RECORDED_ACTIVE = {"running", "detached", "connecting", "waiting_auth"}


def list_sessions(request: dict, root: Path) -> None:
    windows: dict[str, str] | None = None
    for path in (root / "sessions").glob("*.json"):
        record = json.loads(read_private(path))
        if record.get("environment_id") != request.get("environment_id"):
            continue
        status = record.get("status", "unknown")
        if not WINDOWS and record.get("persistent") is False:
            # Started without tmux: it lives and dies with its SSH terminal, so the CLI's pid is the evidence.
            alive = pid_alive(record.get("pid"))
            if alive:
                status = "running"
            elif status in RECORDED_ACTIVE:
                status = "ended" if alive is False else "unknown"
        elif not WINDOWS:
            if windows is None:
                try:
                    windows = tmux_windows()
                except RuntimeError:
                    windows = {}
            if record["session_id"] in windows:
                status = "running"
            elif status in {"running", "detached", "connecting", "waiting_auth"}:
                status = "unknown"  # Server loss/reboot is not proof of a clean CLI exit.
        elif status in RECORDED_ACTIVE:
            status = "unknown"  # Last recorded state alone cannot prove a process is alive.
        emit(request, {"session_id": record["session_id"], "status": status})


# How a CLI that the user quit with Ctrl+C / closed the window / terminated looks as a child exit status
# (shell style 128+signal, or Python's negative signal number). These are deliberate exits, not crashes.
USER_QUIT_CODES = {130, 143, 129, -2, -15, -1}


def run_worker(request: dict, root: Path, env: dict | None = None) -> int:
    shell = is_shell(request)
    env = env or (shell_env() if shell else prepare(request["account"], root))
    if request.get("github_account"):
        env = github_session_env(request["github_account"], root, env)
    stop_path = session_path(root, request["session_id"]).with_suffix(".stop")
    if stop_path.exists():
        save_status(root, request, "ended")
        return 0
    if not shell:
        try:
            summary = sync_user_data(request["account"], root, request["path"], session_id=request["session_id"])
        except Exception:                      # a refresh that fails must never keep the CLI from opening
            summary = {"failed": ["all"]}
        extensions, history = summary.get("extensions") or {}, summary.get("history") or {}
        if summary.get("failed") or extensions.get("skipped") or extensions.get("limited") or history.get("limited"):
            print(tr("Some user data could not be refreshed; starting with what this profile already has."), flush=True)
        if extensions.get("linked"):
            print(tr("Links in this profile were left untouched: {names}. Remove the links (not their targets) so they can be refreshed.",
                     names=", ".join(extensions["linked"])), flush=True)
        mark_onboarded(request["account"], root)
    profile = request.get("terminal_profile")
    if shell and profile and profile.get("profile_env"):
        directory = root / "profiles" / "plugins" / safe_id(profile["id"])
        private_dir(directory)
        # Extension processes inherit no routing or credentials from another agent.
        for key in list(env):
            if key.upper().startswith(("CODEX_", "CLAUDE_", "ANTHROPIC_", "OPENAI_")) or key.upper() == "CLAUDECODE":
                del env[key]
        env[profile["profile_env"]] = str(directory)
    try:
        if not shell and quota_supported(request["account"]):
            result = run_managed_codex(request, root, env, stop_path)
            if result is not None:
                return result
        elif not shell and switch_supported(request["account"]):
            result = run_managed_claude(request, root, env, stop_path)
            if result is not None:
                return result
        command = shell_command(profile) if shell and profile else shell_command() if shell else cli_command(request["account"], "launch", root, "device",
                                                                                                          Path(request["path"]), request.get("resume") is True,
                                                                                                          request.get("resume_thread_id", ""))
        process = subprocess.Popen(command, cwd=request["path"], env=env)
    except Exception:
        save_status(root, request, "failed")  # the record must not stay "waiting for sign-in" forever
        raise
    save_status(root, request, "running", pid=process.pid)
    try:
        while process.poll() is None:
            try:
                if stop_path.exists():
                    if WINDOWS:
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        process.terminate()
                time.sleep(0.3)
            except KeyboardInterrupt:
                # The CLI receives the same console Ctrl+C. It may cancel only
                # the current answer, so keep supervising until it really exits.
                continue
        result = process.wait()
    finally:
        stop_path.unlink(missing_ok=True)
    save_status(root, request, "ended" if result == 0 or result in USER_QUIT_CODES else "failed")
    return result


# Remote exit status meaning "this launch failed before any tmux worker existed". The runner can then
# mark the session failed instead of the conservative "unknown" (which means: might still be running).
EXIT_NOT_STARTED = 78
# After an interactive launch/attach returns, the supervisor knows what happened to the tmux session. It reports it as its
# own exit status, an out-of-band channel the CLI cannot write to (the CLI's terminal output is never trusted as state).
EXIT_SESSION_ENDED = 79
EXIT_SESSION_DETACHED = 80
EXIT_SESSION_FAILED = 81
_worker_created = False


def _mark_worker_created() -> None:
    global _worker_created
    _worker_created = True


def direct_outcome(request: dict, root: Path, result: int) -> int:
    """Outcome of a session that ran directly in the SSH terminal (no tmux): the worker's own record is the evidence."""
    try:
        status = json.loads(read_private(session_path(root, request["session_id"]))).get("status", "unknown")
    except (OSError, ValueError):
        status = "unknown"
    if status not in {"ended", "failed"}:
        status = "unknown"
    emit(request, {"session_id": request["session_id"], "status": status})
    return {"ended": EXIT_SESSION_ENDED, "failed": EXIT_SESSION_FAILED}.get(status, result)


def stop_direct_session(request: dict, root: Path, record: dict, record_path: Path) -> int:
    write_json(record_path.with_suffix(".stop"), {"stop_requested": True})
    if pid_alive(record.get("pid")):
        os.kill(record["pid"], signal.SIGTERM)
    save_status(root, record, "ended")
    emit(request, {"session_id": request["session_id"], "status": "ended"})
    return 0


def session_outcome(request: dict, root: Path, attach_code: int) -> int:
    """Classify the session once ``tmux attach`` returned and map it to an exit status the launcher understands."""
    status = "unknown"
    try:
        if tmux_session_exists(request["session_id"]):
            status = "detached"
        else:
            try:
                status = json.loads(read_private(session_path(root, request["session_id"]))).get("status", "unknown")
            except (OSError, ValueError):
                status = "unknown"
            if status not in {"ended", "failed"}:
                status = "unknown"  # e.g. the host rebooted while the record still said running
    except RuntimeError:
        status = "unknown"  # cannot verify tmux: stay conservative
    emit(request, {"session_id": request["session_id"], "status": status})
    return {"detached": EXIT_SESSION_DETACHED, "ended": EXIT_SESSION_ENDED, "failed": EXIT_SESSION_FAILED}.get(status, attach_code)


def session_action(request: dict, root: Path) -> int:
    """attach to / stop ONE session on this machine."""
    action = request["action"]
    record_path = session_path(root, request["session_id"])
    try:
        record = json.loads(read_private(record_path))
    except FileNotFoundError:
        record = None
    if record and record["environment_id"] != request["environment_id"]:
        raise ValueError("Session environment mismatch")
    if action == "stop" and not WINDOWS and record and record.get("persistent") is False:
        return stop_direct_session(request, root, record, record_path)
    if action == "stop" and not WINDOWS:
        # A launch may still be authenticating and have no tmux worker yet.
        # Persist intent before checking tmux so it cannot start afterwards.
        write_json(record_path.with_suffix(".stop"), {"stop_requested": True})
        window = tmux_windows().get(request["session_id"])
        if window:
            result = tmux("kill-window", "-t", window)
            if result.returncode:
                return result.returncode
        else:
            print("No running tmux session; the launch may have failed before creating a worker.")
        if record:
            save_status(root, record, "ended")
        emit(request, {"session_id": request["session_id"], "status": "ended"})
        return 0
    if not record:
        raise RuntimeError("No remote session record; launch a new session or remove the stale local record")
    if action == "attach" and not WINDOWS and record.get("persistent") is False:
        raise RuntimeError("This session was started without tmux, so it cannot be re-attached. "
                           "Turn on background keep-alive for the project and start it again.")
    if WINDOWS:
        if action == "attach":
            print("Local CLI is in its original terminal. Use that window; only SSH supports reattach.")
        else:
            record_path.with_suffix(".stop").touch(mode=0o600)
            print("Stop requested. The original worker will stop its own CLI process.")
        return 0
    attached = attach_terminal(request)
    return session_outcome(request, root, 1 if attached is None else attached)


BROWSE_LIMIT = 1000


def browse_listing(path: str) -> dict:
    """Sub-folders of ``path`` (``~`` allowed); a path that is not there falls back to its nearest existing parent."""
    target = os.path.normpath(os.path.join(os.path.expanduser("~"), os.path.expanduser(path.strip() or "~")))
    shown = target
    while not os.path.isdir(shown) and os.path.dirname(shown) != shown:
        shown = os.path.dirname(shown)
    names: list[str] = []
    try:
        with os.scandir(shown) as entries:
            for entry in entries:
                try:
                    if entry.is_dir():
                        names.append(entry.name)
                except OSError:
                    continue
    except OSError as error:
        raise RuntimeError(tr('Could not read folder:') + (error.strerror or type(error).__name__)) from error
    names.sort(key=str.casefold)
    parent = os.path.dirname(shown)
    return {"path": shown, "requested": target, "exists": shown == target, "home": os.path.expanduser("~"),
            "parent": "" if parent == shown else parent, "dirs": names[:BROWSE_LIMIT], "truncated": len(names) > BROWSE_LIMIT}


def perform(request: dict, root: Path) -> int:
    validate_request(request)
    private_dir(root)
    # Desktop shortcuts and non-interactive SSH can inherit a PATH without per-user installs.
    os.environ["PATH"] = augmented_path()
    action = request["action"]
    if action in {"github", "harness", "import-settings", "import-history"}:
        if action == "github":
            result = github_action(request, root)
        elif action == "harness":
            result = harness_action(request, root)
        else:
            if action == "import-history" and request.get("project_history") is True:
                result = {"status": "imported", "kind": "project-history",
                          "summary": sync_project_history(request["account"], root, request.get("path", ""))}
            else:
                result = manual_import(request["account"], root, "settings" if action == "import-settings" else "history")
        operation_result(request, root, result)
        return 0
    if action in {"check-tools", "update-tool"}:
        return manage_tools(request, root)
    if action == "codex-control":
        operation_result(request, root, codex_control_action(request, root))
        return 0
    if action == "browse":
        print("VIBE_BROWSE_" + request.get("nonce", "") + ":" + json.dumps(browse_listing(request.get("path", ""))), flush=True)
        return 0
    if action == "quota":
        summary = fetch_quota(request["account"], root, request.get("environment_id", "local"))
        # A manual check also feeds the shared cache that quota flows choose from.
        write_json(quota_file(root, request["account"]["id"], summary["environment_id"]), summary)
        for window in summary["windows"]:
            print(f"{window['window_minutes']} min window: {window['used_percent']}% used")
        print("\nVIBE_QUOTA_" + request.get("nonce", "") + ":" + json.dumps(summary), flush=True)
        return 0
    if action == "doctor":
        doctor(request, root)
        return 0
    if action == "list-sessions":
        list_sessions(request, root)
        return 0
    if action in {"attach", "stop"}:
        if action == "stop" and request.get("session_ids"):
            worst = 0
            for session_id in request["session_ids"]:
                try:
                    code = session_action(dict(request, session_id=session_id), root)
                except (ValueError, RuntimeError, OSError) as error:
                    print(f"{session_id[:8]}: {error}")
                    code = 1
                worst = worst or code
            if request.get("workspace_id") and not WINDOWS:
                worst = worst or end_workspace_tmux(request["workspace_id"])
            return worst
        return session_action(request, root)
    if action == "worker":
        return run_worker(request, root)
    doctor(request, root)
    if action == "login":
        authenticate(request, root, force=True)
        operation_result(request, root, {"status": "configured" if request["account"].get("auth") == "api" else "authenticated"})
        return 0
    path = ensure_project_dir(Path(request["path"]))
    request["path"] = str(path.resolve())
    shell = is_shell(request)
    if not shell and request.get("quota_policy"):
        request = initial_quota_account(request, root)
    if not shell:
        check_settings(request["account"], root, path.resolve())
    stop_path = session_path(root, request["session_id"]).with_suffix(".stop")
    if stop_path.exists():
        save_status(root, request, "ended")
        return 0
    if shell:
        env = shell_env()  # no account, no credentials: just the user's own shell
    else:
        save_status(root, request, "waiting_auth")
        try:
            env = authenticate(request, root)
        except Exception:
            save_status(root, request, "ended" if stop_path.exists() else "failed")
            raise
    if stop_path.exists():
        save_status(root, request, "ended")
        return 0
    if WINDOWS:
        return run_worker(request, root, env)
    if request.get("persistent", True) is False:
        # Opted out of tmux: run the CLI directly in this SSH terminal. Closing the terminal ends it.
        _mark_worker_created()
        return direct_outcome(request, root, run_worker(request, root, env))
    # Persist a nonsecret immutable launch snapshot. tmux gets no API key in argv
    # or its server environment; its worker reads the isolated credential store.
    snapshot = dict(request, action="worker")
    write_json(session_path(root, request["session_id"]), snapshot)
    # Existing tmux servers retain their original PATH. New workers must inherit
    # the current SSH launch PATH so a newly updated installation is selected.
    command = shlex.join(["/usr/bin/env", *utf8_locale(os.environ), "PATH=" + os.environ.get("PATH", ""), sys.executable, "-I",
                          str(Path(__file__).resolve()), "--worker-file", str(session_path(root, request["session_id"]))])
    window = tmux_new_window(tmux_group(request.get("workspace_id", "")), tmux_label(request), request["path"], command)
    if window is None:
        raise RuntimeError("Cannot create tmux session")
    global _worker_created
    _worker_created = True
    if tmux("set-option", "-w", "-t", window, "@vibe-session", request["session_id"], capture=True).returncode:
        tmux("kill-window", "-t", window, capture=True)        # unmarked, the launcher could never find it again
        raise RuntimeError("Cannot create tmux session")
    if stop_path.exists():
        result = tmux("kill-window", "-t", window)
        if result.returncode:
            raise RuntimeError("Stop requested; cannot confirm tmux worker was stopped")
        save_status(root, request, "ended")
        return 0
    emit(request, {"session_id": request["session_id"], "status": "running"})
    attached = attach_terminal(request)
    return session_outcome(request, root, 1 if attached is None else attached)


# --- account quota (Codex subscription: the 5-hour and weekly windows) --------------------------------------------
GAP_KINDS = ("plugin", "app", "mcp", "mcp_unknown")      # account-level differences a user may accept to switch without


@dataclass(frozen=True)
class QuotaPolicy:
    id: str
    name: str
    environment_id: str
    account_ids: tuple[str, ...]
    strategy: str = "priority"
    threshold: float = 10.0
    interval: int = 120
    when_empty: str = "wait"
    enabled: bool = True
    accepted_gaps: dict | None = None   # account id -> ["plugin:name", ...] the user accepted to switch to it without
    flow: dict | None = None        # the block workflow (see flow_issues); None: the template of strategy/when_empty
    plan_id: str = ""               # the saved plan this terminal copy came from, and that plan's revision
    revision: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "QuotaPolicy":
        if not isinstance(data, dict):
            raise ValueError("Invalid quota policy")
        for key in ("id", "name", "environment_id"):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > 128 or not value.isprintable():
                raise ValueError(f"Invalid policy {key}")
        safe_id(data["id"])
        safe_id(data["environment_id"])
        ids = data.get("account_ids")
        if not isinstance(ids, (list, tuple)) or not 1 <= len(ids) <= 128:
            raise ValueError("Select at least one account")
        if any(not isinstance(x, str) or not x or len(x) > 128 or not x.isprintable() for x in ids) or len(set(ids)) != len(ids):
            raise ValueError("Invalid account pool")
        for account_id in ids:
            safe_id(account_id)
        strategy = data.get("strategy", "priority")
        threshold = data.get("threshold", 10.0)
        interval = data.get("interval", 120)
        when_empty = data.get("when_empty", "wait")
        enabled = data.get("enabled", True)
        if strategy not in ("priority", "round-robin", "available-quota"):
            raise ValueError("Invalid quota strategy")
        if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold < 100:
            raise ValueError("Invalid remaining quota threshold")
        if type(interval) is not int or not 60 <= interval <= 86400:
            raise ValueError("Monitoring interval must be between 60 and 86400 seconds")
        if when_empty not in ("wait", "notify-only") or type(enabled) is not bool:
            raise ValueError("Invalid empty-pool action")
        accepted = data.get("accepted_gaps") or {}
        if (not isinstance(accepted, dict) or len(accepted) > 128
                or any(not isinstance(names, list) or len(names) > 64 or not all(
                    isinstance(name, str) and re.fullmatch(r"(%s):[^\x00-\x1f]{1,64}" % "|".join(GAP_KINDS), name)
                    for name in names) for names in accepted.values())):
            raise ValueError("Invalid accepted account differences")
        accepted = {safe_id(key): sorted(set(names)) for key, names in accepted.items() if names} or None
        plan_id, revision = data.get("plan_id", ""), data.get("revision", 0)
        if not isinstance(plan_id, str) or (plan_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", plan_id)):
            raise ValueError("Invalid plan")
        if type(revision) is not int or not 0 <= revision <= 10 ** 9:
            raise ValueError("Invalid plan revision")
        flow = data.get("flow")
        if flow is not None:
            # Only its shape here (control requests carry it through); checked_quota_policy applies the flow's rules.
            text = json.dumps(flow) if isinstance(flow, dict) else ""
            if not text or len(text) > 32768:
                raise ValueError("Invalid quota flow")
            flow = json.loads(text)
        return cls(data["id"], data["name"].strip(), data["environment_id"], tuple(ids), strategy,
                   float(threshold), interval, when_empty, enabled, accepted, flow, plan_id, revision)

    def to_dict(self) -> dict:
        result = asdict(self)
        result["account_ids"] = list(self.account_ids)
        return result


@dataclass(frozen=True)
class QuotaSelection:
    account_id: str | None
    reason: str
    next_retry: float | None = None
    remaining: float | None = None


def _quota_timestamp(value: object) -> float | None:
    if type(value) in (int, float) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def quota_state(snapshot: dict, environment_id: str, now: float, interval: int,
                threshold: float = 0) -> tuple[float | None, float | None]:
    """Only the Codex meter applies; unrelated product buckets cannot disqualify it."""
    if not isinstance(snapshot, dict) or snapshot.get("environment_id") != environment_id or snapshot.get("tool") not in ("codex", "claude"):
        return None, None
    checked = _quota_timestamp(snapshot.get("checked_at"))
    # Claude quota is only seen while one of the account's sessions runs: its last observation counts for an hour and
    # never past a window reset (usage elsewhere can only make it lower; a failed request then moves on).
    passive = snapshot.get("tool") == "claude"
    if checked is None or not 0 <= now - checked <= (3600 if passive else max(2 * interval, 120)):
        return None, None
    if passive and any(not isinstance(window, dict) or (_quota_timestamp(window.get("resets_at")) or 0) <= now
                       for window in snapshot.get("windows") or []):
        return None, None
    buckets = snapshot.get("buckets")
    meter = buckets.get("codex") if isinstance(buckets, dict) else snapshot
    if not isinstance(meter, dict):
        return None, None
    windows = meter.get("windows")
    reached = meter.get("reached")
    if not isinstance(windows, list) or not (windows or isinstance(reached, str) and reached):
        return None, None
    remaining, resets, missing_reset = [], [], False
    for window in windows:
        used = window.get("used_percent") if isinstance(window, dict) else None
        if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
            return None, None
        remaining.append(100 - used)
        reset = _quota_timestamp(window.get("resets_at"))
        if 100 - used <= threshold:
            if reset is not None and reset > now:
                resets.append(reset)
            else:
                missing_reset = True
    # A server-reported limit supersedes apparently spare usage in its windows.
    if isinstance(reached, str) and reached:
        remaining.append(0.0)
        # A reached signal with no blocking window has no attributable reset.
        if not resets:
            missing_reset = True
    return min(remaining), max(resets) if resets and not missing_reset else None


def select_quota_account(policy: QuotaPolicy, accounts: list[dict] | Mapping[str, dict], snapshots: Mapping[str, dict],
           environment_id: str, now: float, current_account_id: str | None = None,
           cursor: str | None = None) -> QuotaSelection:
    policy = QuotaPolicy.from_dict(policy.to_dict())
    if not policy.enabled or policy.environment_id != environment_id:
        return QuotaSelection(None, "disabled" if not policy.enabled else "wrong-environment")
    records = accounts if isinstance(accounts, Mapping) else {a["id"]: a for a in accounts}
    candidates, resets = [], []
    for account_id in policy.account_ids:
        account = records.get(account_id, {})
        if not switch_supported(account) or account.get("environment_id", "local") != environment_id:
            continue
        snapshot = snapshots.get(account_id, {})
        if snapshot.get("account_id", account_id) != account_id:
            continue
        remaining, reset = quota_state(snapshot, environment_id, now, policy.interval, policy.threshold)
        if remaining is not None and remaining > policy.threshold:
            candidates.append((account_id, remaining))
        elif reset is not None:
            resets.append(reset)
    if not candidates:
        return QuotaSelection(None, "no-available-account", min(resets) if resets and policy.when_empty == "wait" else None)
    if current_account_id in {item[0] for item in candidates}:
        account_id, remaining = next(item for item in candidates if item[0] == current_account_id)
        return QuotaSelection(account_id, "keep-current", remaining=remaining)
    if policy.strategy == "available-quota":
        chosen = max(candidates, key=lambda item: item[1])
    elif policy.strategy == "round-robin" and cursor in policy.account_ids:
        start = policy.account_ids.index(cursor) + 1
        ordered = policy.account_ids[start:] + policy.account_ids[:start]
        chosen = min(candidates, key=lambda item: ordered.index(item[0]))
    else:
        chosen = candidates[0]
    return QuotaSelection(chosen[0], "selected", remaining=chosen[1])


def commit_quota_cursor(policy: QuotaPolicy, selected_id: str) -> str:
    """Caller records this only after a successful launch/handoff, never during preview."""
    if selected_id not in policy.account_ids:
        raise ValueError("Account is not in the policy pool")
    return selected_id


def checked_quota_policy(data: dict) -> QuotaPolicy:
    """A policy that may run or be saved: its fields and its flow's rules (flow_issues)."""
    policy = QuotaPolicy.from_dict(data)
    if policy.flow is not None:
        issues = flow_issues(policy.flow, policy.account_ids)
        if issues:
            raise ValueError("Invalid quota flow: " + issues[0][1])
    return policy


def quota_flow(policy: QuotaPolicy) -> dict:
    """The flow a policy runs: its own, or the template of its strategy and empty-pool action."""
    return policy.flow if policy.flow is not None else quota_template(policy.strategy, policy.account_ids, policy.when_empty)


def initial_quota_account(request: dict, root: Path) -> dict:
    """New work starts on the account its flow selects (the first select block's pool and strategy, from the cached
    quota of this environment). Resumed work keeps its conversation's account; without a verified choice the requested
    account starts."""
    if not request.get("quota_policy") or request.get("resume_thread_id") or request.get("resume") is True:
        return request
    policy = checked_quota_policy(request["quota_policy"])
    environment = request.get("environment_id") or "local"
    flow = quota_flow(policy)
    edges = {(edge["from"], edge["branch"]): edge["to"] for edge in flow["edges"]}
    nodes = {node["id"]: node for node in flow["nodes"]}
    queue_ = [next(key for key, node in nodes.items() if node["type"] == "start")]
    select = None
    while queue_ and select is None:
        node = nodes[queue_.pop(0)]
        if node["type"] == "select":
            select = node
        queue_ += [edges[(node["id"], branch)] for branch in FLOW_BRANCHES.get(node["type"], ())]
    if not policy.enabled or select is None:
        return request
    accounts = {account["id"]: account for account in request.get("quota_accounts", [])}
    snapshots = {}
    for identifier in select["accounts"]:
        try:
            snapshots[identifier] = json.loads(read_private(quota_file(root, identifier, environment), limit=65536))
        except (OSError, ValueError):
            pass
    chosen = select_quota_account(replace(policy, account_ids=tuple(select["accounts"]), strategy=select["strategy"]),
                                  accounts, snapshots, environment, time.time(), cursor=request.get("quota_cursor")).account_id
    if not chosen or chosen == request["account"]["id"] or chosen not in accounts:
        return request
    return dict(request, account=dict(accounts[chosen]), quota_cursor=chosen)


# --- the block workflow: declarative data the editor draws and the worker evaluates --------------------------------
# A flow is a finite acyclic graph. It runs at every quota check and ends in one outcome: hand off to an account (only
# through a "select" block, whose "found" branch always leads to the one safe-handoff block a flow with select blocks
# has), wait and continue after a verified recovery ("wait", shown as Auto continue), notify only, or keep the current
# account. Waiting is a state the worker schedules, never a loop.
FLOW_KINDS = ("start", "condition", "select", "handoff", "wait", "notify", "keep")
FLOW_BRANCHES = {"start": ("next",), "condition": ("yes", "no"), "select": ("found", "none")}
FLOW_TESTS = ("low", "exhausted", "available")
FLOW_STRATEGIES = ("priority", "round-robin", "available-quota")
FLOW_LIMIT = 32


def quota_template(strategy: str, account_ids, when_empty: str = "wait") -> dict:
    """The three ready-made plans: if this account is low, select the next one by ``strategy``; none left: wait or notify."""
    return {"nodes": [{"id": "start", "type": "start", "x": 0, "y": 100},
                      {"id": "low", "type": "condition", "test": "low", "x": 210, "y": 100},
                      {"id": "select", "type": "select", "accounts": list(account_ids), "strategy": strategy, "x": 420, "y": 20},
                      {"id": "handoff", "type": "handoff", "x": 640, "y": 0},
                      {"id": "empty", "type": "wait" if when_empty == "wait" else "notify", "x": 640, "y": 110},
                      {"id": "keep", "type": "keep", "x": 420, "y": 210}],
            "edges": [{"from": "start", "branch": "next", "to": "low"},
                      {"from": "low", "branch": "yes", "to": "select"},
                      {"from": "low", "branch": "no", "to": "keep"},
                      {"from": "select", "branch": "found", "to": "handoff"},
                      {"from": "select", "branch": "none", "to": "empty"}]}


def flow_link_issue(kind: str, branch: str, target: str, same: bool = False) -> str | None:
    """Why ``branch`` of a ``kind`` block may not lead to a ``target`` block: "edge", "handoff" or None."""
    if branch not in FLOW_BRANCHES.get(kind, ()) or target not in FLOW_KINDS or target == "start" or same:
        return "edge"
    # a found account always goes through the safe handoff, and only a found account does
    return "handoff" if (kind == "select" and branch == "found") != (target == "handoff") else None


def flow_issues(flow: object, pool: tuple) -> list[tuple[str, str]]:
    """Every problem that keeps a flow from running, as (block id or "", code); empty when it is valid. Codes: invalid,
    start, handoff, test, accounts, strategy, edge, unconnected, cycle, unreachable, unguarded."""
    if not isinstance(flow, dict) or set(flow) != {"nodes", "edges"} or not isinstance(flow["nodes"], list) \
            or not isinstance(flow["edges"], list) or len(flow["nodes"]) > FLOW_LIMIT or len(flow["edges"]) > 2 * FLOW_LIMIT:
        return [("", "invalid")]
    nodes, issues = {}, []
    for node in flow["nodes"]:
        if not isinstance(node, dict) or node.keys() - {"id", "type", "test", "accounts", "strategy", "x", "y"}:
            return [("", "invalid")]
        ident = node.get("id")
        if (not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", ident) or ident in nodes
                or node.get("type") not in FLOW_KINDS
                or any(key in node and (type(node[key]) is not int or not 0 <= node[key] <= 10000) for key in ("x", "y"))):
            return [("", "invalid")]
        kind = node["type"]
        if kind == "condition" and node.get("test") not in FLOW_TESTS:
            issues.append((ident, "test"))
        if kind == "select":
            accounts = node.get("accounts")
            if (not isinstance(accounts, list) or not accounts or len(accounts) > 128
                    or not all(isinstance(item, str) and item in pool for item in accounts) or len(set(accounts)) != len(accounts)):
                issues.append((ident, "accounts"))
            if node.get("strategy") not in FLOW_STRATEGIES:
                issues.append((ident, "strategy"))
        nodes[ident] = node
    kinds = [node["type"] for node in nodes.values()]
    if kinds.count("start") != 1:
        issues.append(("", "start"))
    if kinds.count("handoff") != (1 if "select" in kinds else 0):
        issues.append(("", "handoff"))
    edges = {}
    for edge in flow["edges"]:
        if not isinstance(edge, dict) or set(edge) != {"from", "branch", "to"}:
            return [("", "invalid")]
        source, branch, target = edge["from"], edge["branch"], edge["to"]
        if (source not in nodes or target not in nodes
                or flow_link_issue(nodes[source]["type"], branch, nodes[target]["type"], source == target) == "edge"
                or (source, branch) in edges):
            issues.append((source if isinstance(source, str) and source in nodes else "", "edge"))
            continue
        edges[(source, branch)] = target
    for ident, node in nodes.items():
        for branch in FLOW_BRANCHES.get(node["type"], ()):
            target = edges.get((ident, branch))
            if target is None:
                issues.append((ident, "unconnected"))
            elif flow_link_issue(node["type"], branch, nodes[target]["type"]):
                issues.append((ident, "handoff"))
    if issues:
        return issues
    start = next(ident for ident, node in nodes.items() if node["type"] == "start")
    following = {ident: [edges[(ident, branch)] for branch in FLOW_BRANCHES.get(node["type"], ())] for ident, node in nodes.items()}
    state = {}

    def visit(ident: str) -> bool:
        state[ident] = 1
        for target in following[ident]:
            if state.get(target) == 1 or (state.get(target) is None and visit(target)):
                return True
        state[ident] = 2
        return False

    for ident in nodes:
        if state.get(ident) is None and visit(ident):
            return [(ident, "cycle")]
    reached, unguarded, stack = {start}, {start}, [start]
    while stack:
        ident = stack.pop()
        for target in following[ident]:
            if target not in reached:
                reached.add(target)
                stack.append(target)
    stack = [start]
    while stack:
        # A healthy conversation keeps its account: every way to a handoff passes a low-quota or exhausted "yes".
        ident = stack.pop()
        node = nodes[ident]
        for branch in FLOW_BRANCHES.get(node["type"], ()):
            target = edges[(ident, branch)]
            if node["type"] == "condition" and node["test"] in ("low", "exhausted") and branch == "yes":
                continue
            if target not in unguarded:
                unguarded.add(target)
                stack.append(target)
    issues = [(ident, "unreachable") for ident, node in nodes.items() if ident not in reached and node["type"] != "handoff"]
    issues += [(ident, "unguarded") for ident, node in nodes.items() if ident in unguarded and node["type"] == "select"]
    return issues


def flow_outcome(flow: dict, facts: Callable[[str], bool], choose: Callable[[tuple, str], str | None],
                 trace: list | None = None) -> tuple[str, str | None]:
    """Walk a valid flow: ("handoff", account), or ("wait" | "notify" | "keep", None). ``facts`` answers a condition,
    ``choose`` a select block (an eligible account other than the current one, or None); ``trace`` collects the path."""
    nodes = {node["id"]: node for node in flow["nodes"]}
    edges = {(edge["from"], edge["branch"]): edge["to"] for edge in flow["edges"]}
    ident = next(key for key, node in nodes.items() if node["type"] == "start")
    for _ in range(FLOW_LIMIT + 1):                 # acyclic: every walk ends within the number of blocks
        node = nodes[ident]
        if trace is not None:
            trace.append(ident)
        if node["type"] == "start":
            ident = edges[(ident, "next")]
        elif node["type"] == "condition":
            ident = edges[(ident, "yes" if facts(node["test"]) else "no")]
        elif node["type"] == "select":
            chosen = choose(tuple(node["accounts"]), node["strategy"])
            if chosen:
                handoff = edges[(ident, "found")]
                if trace is not None:
                    trace.append(handoff)
                return "handoff", chosen
            ident = edges[(ident, "none")]
        else:
            return node["type"], None
    raise ValueError("Invalid quota flow")


QUOTA_ERRORS = ("usageLimitExceeded", "rateLimitExceeded")
# The nonsecret account fields a control request may carry; other launcher metadata never leaves the desktop.
CONTROL_ACCOUNT_FIELDS = ("id", "name", "tool", "auth", "environment_id", "model", "share_project_history")
CONTROL_COMMAND_MAX_AGE = 300      # a queued request older than this is answered, never executed late
CONTROL_HEARTBEAT = 5              # the worker proves it is alive this often ...
CONTROL_HEARTBEAT_STALE = 30       # ... and is no longer trusted to be alive after this
QUOTA_LEASE_STALE = 180            # a lease older than any quota query belongs to a monitor that died


def validate_control_accounts(accounts: object, environment: str) -> set:
    if not isinstance(accounts, list) or len(accounts) > 128:
        raise ValueError("Invalid Codex account pool")
    seen = set()
    for account in accounts:
        if not isinstance(account, dict) or account.keys() - set(CONTROL_ACCOUNT_FIELDS):
            raise ValueError("Control account metadata contains unsupported fields")
        name = account.get("name", "")
        if (not isinstance(name, str) or len(name) > 128 or not name.isprintable()
                or type(account.get("share_project_history", False)) is not bool):
            raise ValueError("Invalid account metadata")
        validate_request({"action": "quota", "account": account, "environment_id": environment})
        if not switch_supported(account) or not account_fits(account, environment) or account["id"] in seen:
            raise ValueError("Select distinct subscription accounts in this environment")
        seen.add(account["id"])
    if len({account["tool"] for account in accounts}) > 1:
        raise ValueError("A pool holds accounts of one CLI")
    return seen


def validate_codex_control(request: dict) -> None:
    safe_id(request.get("session_id"))
    if request.get("control_action") not in {"status", "switch", "policy", "cancel", "resume"}:
        raise ValueError("Unsupported Codex control action")
    if request.get("control_action") == "switch":
        safe_id(request.get("target_account_id"))
    if "allow_gaps" in request and (type(request["allow_gaps"]) is not bool or request.get("control_action") != "switch"):
        raise ValueError("Invalid account difference choice")
    if "align_rules" in request and (type(request["align_rules"]) is not bool or request.get("control_action") != "switch"):
        raise ValueError("Invalid approval rule choice")
    environment = request.get("environment_id") or "local"
    seen = validate_control_accounts(request.get("accounts", []), environment)
    policy = request.get("policy")
    if policy is not None:
        policy = QuotaPolicy.from_dict(policy)
        if policy.environment_id != environment or not set(policy.account_ids) <= seen:
            raise ValueError("Quota policy accounts do not belong to this environment")


def codex_control_path(root: Path, session_id: str) -> Path:
    return root / "codex-controls" / safe_id(session_id)


def windows_process_alive(pid) -> bool | None:
    """Does this Windows process still run? None when it cannot be told."""
    if type(pid) is not int or pid <= 0:
        return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False if ctypes.get_last_error() == 87 else None    # ERROR_INVALID_PARAMETER: no such process
    try:
        code = wintypes.DWORD()
        return code.value == 259 if kernel.GetExitCodeProcess(handle, ctypes.byref(code)) else None   # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def process_alive(pid) -> bool | None:
    return windows_process_alive(pid) if WINDOWS else pid_alive(pid)


def control_heartbeat_age(folder: Path) -> float | None:
    try:
        assert_private_path(folder / "heartbeat")
        return time.time() - (folder / "heartbeat").stat().st_mtime
    except (OSError, ValueError):
        return None


def control_worker_alive(folder: Path, record: dict) -> bool:
    """A worker counts as alive only while its heartbeat is fresh and its process, where that can be checked, exists:
    a terminal that was closed (its whole process tree ends with it) is seen as gone at once."""
    age = control_heartbeat_age(folder)
    return (age is not None and -CONTROL_HEARTBEAT_STALE <= age <= CONTROL_HEARTBEAT_STALE
            and process_alive(record.get("worker_pid")) is not False)


def codex_control_status(root: Path, session_id: str, environment_id: str) -> dict:
    folder = codex_control_path(root, session_id)
    try:
        record = json.loads(read_private(folder / "state.json", limit=65536))
    except FileNotFoundError:
        return {"status": "unmanaged", "message": tr("Reopen this terminal to enable safe account management.")}
    if not isinstance(record, dict) or record.get("environment_id") != environment_id:
        raise ValueError("Control state belongs to another environment")
    if record.get("status") not in {"closed", "unmanaged"} and not control_worker_alive(folder, record):
        return dict(record, status="needs_attention", can_switch=False, worker_alive=False,
                    message=tr("Worker status is unknown; inspect the saved conversation before reopening."))
    return record


def codex_control_action(request: dict, root: Path) -> dict:
    validate_codex_control(request)
    state = codex_control_status(root, request["session_id"], request.get("environment_id") or "local")
    if request["control_action"] == "status":
        return state
    if state["status"] in {"unmanaged", "closed"} or state.get("worker_alive") is False:
        return dict(state, accepted=False)
    folder = codex_control_path(root, request["session_id"])
    command_id = uuid.uuid4().hex
    command = {key: request[key] for key in ("control_action", "target_account_id", "allow_gaps", "align_rules", "policy", "accounts",
                                             "environment_id", "session_id") if key in request}
    command.update(id=command_id, created=time.time())
    write_json(folder / "commands" / (command_id + ".json"), command)
    deadline = time.monotonic() + 5
    result_path = folder / "replies" / (command_id + ".json")
    while time.monotonic() < deadline:
        try:
            reply = json.loads(read_private(result_path, limit=65536))
        except FileNotFoundError:
            time.sleep(0.1)
            continue
        result_path.unlink(missing_ok=True)
        return reply
    # The command stays queued under its ID and runs once; a retry would be a second, different request.
    return dict(state, status="queued", message=tr("Control request queued; refresh status before retrying."))


def managed_codex_command(account: dict, root: Path, executable: str, settings: dict) -> list[str]:
    # Every owner of the thread must see the same conversation index: Codex's own when there is one, as normal launches use.
    state = native_codex_state()
    if state is None:
        state = root / "codex-state"
        private_dir(state)
    command = [executable, "app-server", "--stdio", "-c", "sqlite_home=" + json.dumps(str(state)),
               "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"']
    model = settings.get("model") or account.get("model")
    if model:
        command += ["-c", "model=" + json.dumps(model)]
    if settings.get("reasoningEffort"):
        command += ["-c", "model_reasoning_effort=" + json.dumps(settings["reasoningEffort"])]
    return command


def managed_codex_supported(executable: str, env: dict, root: Path) -> bool:
    """The native TUI must accept a remote app-server and the app-server a stdio transport; otherwise run it unmanaged."""
    try:
        for args, needles in ((["--help"], (b"--remote", b"--remote-auth-token-env")), (["app-server", "--help"], (b"--stdio",))):
            probe = subprocess.run([executable] + args, env=env, cwd=root, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=30)
            if probe.returncode or not all(needle in probe.stdout for needle in needles):
                return False
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def codex_profile_fingerprint(account: dict, root: Path) -> dict:
    """Compare only shareable extensions/settings; credentials are never opened."""
    profile = profile_dir(root, account)
    result = {}
    for relative in _shareable_files(profile, "codex"):
        if Path(relative).parts[0] in {"plugins", "rules"}:
            # Plugin caches keep old versions and account-linked apps; what Codex really loads is compared through
            # skills/list and app/installed. Approval rules are compared on their own (codex_rule_differences).
            continue
        data = read_private_bytes(profile / relative)
        if len(data) > SHARE_MAX_FILE:
            raise ValueError("An extension exceeds the safe comparison limit")
        result[str(relative)] = hashlib.sha256(data).hexdigest()
    config = profile / "config.toml"
    if config.is_file():
        # Reuse the existing extension-only reduction, never expose configuration values.
        result["config.toml"] = hashlib.sha256(shared_codex_config(read_private(config)).encode()).hexdigest()
    return result


def codex_rule_differences(source: Path, target: Path) -> list[str]:
    """Names of the approval-rule files that differ between two profiles. Read only: rules are never merged, so a switch
    that is refused or cancelled leaves both accounts' approvals exactly as they were."""
    names = sorted({path.name for profile in (source, target) for path in (profile / "rules").glob("*.rules")
                    if _plain(path)})
    if len(names) > 32:
        raise CodexControlError(tr("Too many approval rule files to compare; align them by hand"))
    differing = []
    for name in names:
        contents = [read_private_bytes(path) if path.is_file() else None
                    for path in (source / "rules" / name, target / "rules" / name)]
        if any(content is not None and len(content) > SHARE_MAX_FILE for content in contents):
            raise CodexControlError(tr("Approval rules {name} cannot be compared safely; align them by hand", name=name))
        normal = [None if content is None else content.replace(b"\r\n", b"\n").rstrip(b"\n") for content in contents]
        if normal[0] != normal[1]:
            differing.append(name)
    return differing


def align_codex_rules(source: Path, target: Path, backup: Path) -> list[str]:
    """Only on the user's explicit choice: give the target profile the source's approval rules. Every target file that
    changes is first copied under ``backup`` so it can be restored; the source is never written."""
    names = codex_rule_differences(source, target)
    for name in names:
        if (target / "rules" / name).is_file():
            _write_private(backup / name, read_private_bytes(target / "rules" / name))
    for name in names:
        wanted, current = source / "rules" / name, target / "rules" / name
        if wanted.is_file():
            _write_private(current, read_private_bytes(wanted))
        else:
            current.unlink(missing_ok=True)
    return names


def account_in_use(root: Path, account_id: str, session_id: str) -> bool:
    """Does another live managed terminal on this host run (or switch to) this account?"""
    for folder in list((root / "codex-controls").glob("*"))[:4096]:
        if folder.name == session_id:
            continue
        try:
            record = json.loads(read_private(folder / "state.json", limit=65536))
        except (OSError, ValueError):
            continue
        if (isinstance(record, dict) and account_id in (record.get("account_id"), record.get("pending_account"))
                and record.get("status") not in {"closed", "unmanaged"} and control_worker_alive(folder, record)):
            return True
    return False


def _skill_inventory(answer: dict) -> dict:
    skills = {}
    for entry in answer.get("data") or []:
        for skill in (entry.get("skills") or []) if isinstance(entry, dict) else []:
            if not isinstance(skill, dict) or not isinstance(skill.get("name"), str) or skill.get("pluginId"):
                continue            # a plugin's skills load in the background; its installed version is compared instead
            try:
                with open(skill.get("path", ""), "rb") as stream:
                    content = hashlib.sha256(stream.read(SHARE_MAX_FILE + 1)).hexdigest()
            except (OSError, TypeError, ValueError):
                content = None
            skills[(skill["name"], skill.get("pluginId"), skill.get("scope"))] = (skill.get("enabled"), content)
    return skills


def _enabled_plugins(answer: dict) -> dict:
    return {plugin["id"]: plugin.get("localVersion") or plugin.get("version")
            for marketplace in answer.get("marketplaces") or [] if isinstance(marketplace, dict)
            for plugin in marketplace.get("plugins") or []
            if isinstance(plugin, dict) and isinstance(plugin.get("id"), str) and plugin.get("installed") and plugin.get("enabled")}


def _callable_apps(answer: dict) -> dict:
    return {app["id"]: app.get("runtimeName") or app["id"] for app in answer.get("apps") or []
            if isinstance(app, dict) and isinstance(app.get("id"), str) and app.get("enabled") and app.get("callable")}


def _instruction_identity(value: object, profile: Path) -> tuple:
    if not isinstance(value, str) or not value or Path(value).name.lower() in {"auth.json", ".credentials.json", "api-key"}:
        raise ValueError("Unexpected instruction source")
    path = Path(value)
    try:
        where = ("profile", path.resolve().relative_to(profile.resolve()).as_posix())
    except ValueError:
        where = ("path", os.path.normcase(os.path.abspath(value)))
    with open(path, "rb") as stream:
        data = stream.read(SHARE_MAX_FILE + 1)
    if len(data) > SHARE_MAX_FILE:
        raise ValueError("Instruction source exceeds the comparison limit")
    return where + (hashlib.sha256(data).hexdigest(),)


def instruction_sources_match(source: object, target: object, source_profile: Path, target_profile: Path) -> bool:
    """Profile instruction files sit at different paths; they match when they are the same files with the same content."""
    if not isinstance(source, list) or not isinstance(target, list) or len(source) != len(target) or len(source) > 64:
        return False
    try:
        return ([_instruction_identity(item, source_profile) for item in source]
                == [_instruction_identity(item, target_profile) for item in target])
    except (OSError, ValueError):
        return False


def observed_quota(cached: dict | None, bridge: dict, environment: str) -> dict | None:
    """Merge the sparse ``account/rateLimits/updated`` values over the last full query. Each window keeps the time it was
    really observed, so an old event never looks fresh and a window an event left out is never refreshed by it."""
    limits = bridge.get("rate_limits")
    if not isinstance(limits, dict) or not limits:
        return cached
    stamp = bridge.get("rate_limits_checked_at")
    times = bridge.get("rate_limit_times") or {key: stamp for key in limits}
    base = cached if isinstance(cached, dict) and cached.get("tool") == "codex" else {}
    base_time = _quota_timestamp(base.get("checked_at"))
    buckets = base.get("buckets")
    meter = buckets.get("codex") if isinstance(buckets, dict) else base
    meter = meter if isinstance(meter, dict) else {}
    windows = {}
    if base_time is not None:
        for window in meter.get("windows") or []:
            if isinstance(window, dict):
                windows[window.get("slot")] = (window, base_time)
    reached = (meter.get("reached"), base_time)
    event = quota_summary({"rateLimits": limits})
    for window in event["windows"]:
        at = _quota_timestamp(times.get(window["slot"]))
        if at is not None and (base_time is None or at > base_time):
            windows[window["slot"]] = (window, at)
    at = _quota_timestamp(times.get("rateLimitReachedType"))
    if event["reached"] and at is not None and (base_time is None or at > base_time):
        reached = (event["reached"], at)
    stamps = [moment for _, moment in windows.values()] + ([reached[1]] if reached[0] and reached[1] is not None else [])
    if not stamps:
        return cached or None
    return {"tool": "codex", "account_id": bridge.get("account"), "environment_id": environment, "checked_at": min(stamps),
            "buckets": {"codex": {"windows": [window for window, _ in windows.values()], "reached": reached[0]}}}


class CodexQuotaMonitor:
    """Finite per-worker polling with a shared environment/account cache and lease."""
    def __init__(self, root: Path, environment_id: str, passive: bool = False):
        self.root, self.environment, self.passive = root, environment_id, passive
        self._stop = threading.Event()
        self._thread = None
        self._policy = None
        self._accounts = {}
        self._lock = threading.Lock()
        self._next = {}
        self._failures = {}
        self._due = {}
        self._read = {}             # path -> (mtime, value): the controller asks often, the files change rarely

    def configure(self, policy: QuotaPolicy | None, accounts: dict) -> None:
        with self._lock:
            self._policy, self._accounts = policy if policy and policy.enabled else None, dict(accounts)
            if self._policy is None:
                self._due.clear()
            if self._policy and self._thread is None and not self.passive:
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def require_after(self, account_ids, moment: float) -> None:
        """A trusted reset time: an answer from before it no longer counts, and the account is asked again soon after."""
        with self._lock:
            for identifier in account_ids:
                self._due[identifier] = max(self._due.get(identifier, 0), moment)
                if not self._failures.get(identifier):
                    self._next[identifier] = time.monotonic() + max(0.0, moment - time.time())

    def snapshots(self) -> dict:
        with self._lock:
            ids = self._policy.account_ids if self._policy else ()
        results = {}
        for identifier in ids:
            path = quota_file(self.root, identifier, self.environment)
            try:
                stamp = path.stat().st_mtime_ns
                cached = self._read.get(path)
                if cached is None or cached[0] != stamp:
                    cached = (stamp, json.loads(read_private(path, limit=65536)))
                    self._read[path] = cached
                if isinstance(cached[1], dict):
                    results[identifier] = copy.deepcopy(cached[1])
            except (OSError, ValueError):
                self._read.pop(path, None)
        return results

    def _fresh(self, path: Path, identifier: str, interval: int) -> bool:
        try:
            checked = _quota_timestamp(json.loads(read_private(path, limit=65536)).get("checked_at"))
        except (OSError, ValueError, AttributeError):
            return False
        with self._lock:
            due = self._due.get(identifier, 0)
        return checked is not None and checked >= due and 0 <= time.time() - checked < interval

    @staticmethod
    def _release_stale(lease: Path) -> None:
        try:
            age = time.time() - lease.stat().st_mtime
            try:
                owner = json.loads(read_private(lease, limit=4096)).get("pid")
            except (ValueError, AttributeError):
                owner = None            # still being written, or left half-written by a monitor that died
            if age > QUOTA_LEASE_STALE or (owner is not None and pid_alive(owner) is False):
                lease.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _backoff_until(path: Path) -> tuple[float, int]:
        """The shared failure cool-down of one account in one environment: every terminal waits it out together."""
        try:
            data = json.loads(read_private(path.with_suffix(".backoff"), limit=4096))
            until, count = data.get("until"), data.get("count")
            if type(until) in (int, float) and type(count) is int and 0 < count <= 6 and until - time.time() <= 3600:
                return float(until), count
        except (OSError, ValueError, AttributeError):
            pass
        return 0.0, 0

    def _poll(self, account: dict, interval: int) -> None:
        identifier = account["id"]
        path = quota_file(self.root, identifier, self.environment)
        if self._fresh(path, identifier, interval):
            return
        until, _ = self._backoff_until(path)
        if until > time.time():
            with self._lock:
                self._next[identifier] = time.monotonic() + (until - time.time())
            return
        private_dir(path.parent)
        lease = path.with_suffix(".lease")
        assert_private_path(lease)
        try:
            # Exclusive creation deduplicates monitors in other terminals on this host.
            descriptor = os.open(lease, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            self._release_stale(lease)
            return
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                protect_path(lease)
                json.dump({"pid": os.getpid()}, stream)
            # Another monitor may have completed between our cache read and lease.
            if self._fresh(path, identifier, interval):
                return
            started = time.time()
            summary = fetch_quota(account, self.root, self.environment)
            if (not isinstance(summary, dict) or summary.get("tool") != "codex" or summary.get("account_id") != identifier
                    or summary.get("environment_id") != self.environment):
                raise ValueError("Quota answer belongs to another account")
            # Stamp the moment the question was asked: the answer is at least this recent, never more.
            summary["checked_at"] = datetime.fromtimestamp(started, timezone.utc).isoformat()
            write_json(path, summary)
            path.with_suffix(".backoff").unlink(missing_ok=True)
            with self._lock:
                self._failures[identifier] = 0
        except Exception:
            count = min(max(self._backoff_until(path)[1], self._failures.get(identifier, 0)) + 1, 6)
            delay = min(interval * 2 ** count, 3600)
            try:
                write_json(path.with_suffix(".backoff"), {"until": time.time() + delay, "count": count})
            except OSError:
                pass
            with self._lock:
                self._failures[identifier] = count
                self._next[identifier] = time.monotonic() + delay
        finally:
            lease.unlink(missing_ok=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                policy, accounts = self._policy, dict(self._accounts)
            if policy:
                for identifier in policy.account_ids:
                    if self._stop.is_set():
                        break
                    with self._lock:
                        if self._policy is not policy:
                            break
                        due = time.monotonic() >= self._next.get(identifier, 0)
                    account = accounts.get(identifier)
                    if account and due:
                        self._poll(account, policy.interval)
                        with self._lock:
                            self._next[identifier] = max(self._next.get(identifier, 0), time.monotonic() + policy.interval)
            self._stop.wait(1)

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=35)


class ManagedCodexSession:
    """Worker-owned control and policy state; commands never derive from PTY text."""
    PASSIVE_QUOTA = False           # True: quota is only observed (Claude), never queried
    CONTINUE_GRACE = 120            # passive quota: how long after a reset the CLI's own continuation may take

    def __init__(self, request: dict, root: Path, bridge):
        self.request, self.root, self.bridge = request, root, bridge
        self.folder = codex_control_path(root, request["session_id"])
        self.environment = request.get("environment_id") or "local"
        self.accounts = {request["account"]["id"]: request["account"]}
        self.policy = None
        self.cursor = None              # round-robin progress of this terminal: moves only after a successful switch
        self.waiting = False
        self.next_retry = None
        self.wait_reset = None
        self.notice = ""
        self._wait_mode = "wait"        # the flow outcome that set ``waiting``: "wait" (verify and resume) or "notify"
        self._continue = False
        self._settled = None            # last_error_at of a quota failure already handed off or continued
        self._version = 0               # every applied (or removed) flow is a new version ...
        self._automatic = None          # ... and an automatic switch request remembers (target, version)
        self._stale_handoff = False     # an automatic handoff of an older version was already running when it changed
        self._attached, self._attached_checked = True, None
        self._allow_gaps = set()        # targets the user chose to switch to without their missing capabilities
        self._align_rules = set()       # targets whose approval rules the user chose to align with the current ones
        self._gaps_accepted = []
        self._skip = {}                 # policy targets blocked by account differences -> time to try again
        self._blocked_seen = None
        self.simulated = False          # a test's simulated quota is in effect (scripts/simulate_quota.py)
        self._last = None
        self._handled = set()
        self._target = None
        self._closed = threading.Event()
        self.monitor = CodexQuotaMonitor(root, self.environment, self.PASSIVE_QUOTA)
        private_dir(self.folder / "commands")
        private_dir(self.folder / "replies")
        write_json(self.folder / "heartbeat", {"pid": os.getpid()})
        threading.Thread(target=self._heartbeat, daemon=True).start()
        if request.get("quota_policy"):
            self.apply_policy(request["quota_policy"], request.get("quota_accounts", []))
            if self.policy and request.get("quota_cursor") in self.policy.account_ids:
                self.cursor = request["quota_cursor"]       # an exact reopen keeps its rotation
        self._publish()

    def _heartbeat(self) -> None:
        while not self._closed.wait(CONTROL_HEARTBEAT):
            try:
                os.utime(self.folder / "heartbeat")
            except OSError:
                pass

    def status(self) -> dict:
        state = self.bridge.snapshot()
        status = state["state"]
        if status == "unknown" and not state["thread_id"]:
            status = "starting"
        message = state["reason"]
        if self.waiting and status == "idle":
            status = "waiting_quota"
            message = (tr("Waiting for quota to recover; the task continues automatically.")
                       if self._wait_mode == "wait" and self.next_retry is not None
                       else tr("Quota ran out; continue the task in the terminal when quota is available."))
        elif self.notice and status in {"idle", "running"}:
            message = self.notice
        elif self.policy and not self._attached and status in {"idle", "running"}:
            message = tr("Automatic switching is paused while no terminal shows this session.")
        if self.simulated:
            message = (tr("Simulated quota (test)") + " · " + message).rstrip(" ·")
        return {"status": status, "message": message, "account_id": state["account"], "thread_id": state["thread_id"],
                "simulated": self.simulated,
                "can_switch": state["can_switch"], "pending_account": state["pending_account"],
                "policy": self.policy.to_dict() if self.policy else None, "next_retry": self.next_retry,
                "environment_id": self.environment, "worker_pid": os.getpid(), "backend_pid": state.get("backend_pid"),
                "blocked": state.get("blocked"), "cursor": self.cursor, "paused": bool(self.policy) and not self._attached}

    def apply_policy(self, policy: dict | None, accounts: list) -> None:
        validate_control_accounts(accounts, self.environment)
        if any(account.get("tool") != self.request["account"]["tool"] for account in accounts):
            raise ValueError("Switch between accounts of the same CLI")
        policy = checked_quota_policy(policy) if policy else None
        if policy and policy.environment_id != self.environment:
            raise ValueError("Quota policy belongs to another environment")
        for account in accounts:
            self.accounts[account["id"]] = dict(self.accounts.get(account["id"], {}), **account)
        self._version += 1
        self._cancel_automatic()        # the old flow's queued switch and continuation never run after a change
        self.policy = policy if policy and policy.enabled else None
        self.waiting, self.next_retry, self.wait_reset = False, None, None
        if not self._stale_handoff:
            self.notice = ""
        self._skip = {}                 # a changed flow is a new decision about blocked accounts
        if self.cursor is not None and (not self.policy or self.cursor not in self.policy.account_ids):
            self.cursor = None
        self.monitor.configure(self.policy if self._attached else None, self.accounts)

    def _cancel_automatic(self) -> None:
        """Withdraw an automatic switch that has not started. One already handing off finishes its safe sequence (it
        cannot be cut in the middle) but does not continue the task automatically afterwards."""
        self._continue = False
        automatic, self._automatic = self._automatic, None
        if not automatic or self.bridge.snapshot()["pending_account"] != automatic[0]:
            return
        try:
            self.bridge.cancel_switch()
        except CodexControlError:
            self._stale_handoff = True
            self.notice = tr("Automatic switching stops after the handoff in progress finishes.")

    def _terminal_attached(self) -> bool:
        """A worker in tmux with no client showing its window (tab closed, connection lost) pauses its flow: no quota
        queries, no automatic switch. It resumes when tmux reports a client again. Unknown counts as detached."""
        if not os.environ.get("TMUX") or WINDOWS:
            return True
        moment = time.monotonic()
        if self._attached_checked is not None and moment - self._attached_checked < 2:
            return self._attached
        self._attached_checked = moment
        try:
            window = tmux_windows().get(self.request["session_id"])
            result = tmux("list-windows", "-a", "-F", "#{window_id} #{session_attached}", capture=True)
            lines = (result.stdout or b"").decode("ascii", "replace").split("\n") if not result.returncode else []
            return bool(window) and any(line.split(" ")[0] == window and line.split(" ")[-1].strip() not in ("", "0")
                                        for line in lines)
        except Exception:
            return False

    def _follow_attachment(self, fresh: bool = False) -> None:
        if fresh or self._automatic:
            self._attached_checked = None       # an automatic switch is decided and carried out on a fresh answer
        attached = self._terminal_attached()
        if attached == self._attached:
            return
        self._attached = attached
        if not attached:
            self._cancel_automatic()
        self.monitor.configure(self.policy if attached else None, self.accounts)

    def validate_target(self, identifier: str, settings: dict) -> None:
        account = self.accounts.get(identifier)
        if not account or not quota_supported(account) or not account_fits(account, self.environment):
            raise CodexControlError(tr("Target account is outside this environment"))
        env = prepare(account, self.root)
        if not check_login(account, self.root, env):
            raise CodexControlError(tr("Sign in to the target account before switching"))
        # Entries the refresh skips are skipped for every profile alike; the comparison below decides.
        if sync_user_data(account, self.root, self.request["path"]).get("failed"):
            raise CodexControlError(tr("Target extensions could not be verified"))
        current = self.accounts[self.bridge.snapshot()["account"]]
        self._check_rules(identifier, current, account)
        source = codex_profile_fingerprint(current, self.root)
        target = codex_profile_fingerprint(account, self.root)
        differing = sorted({"/".join(Path(key).parts[:3]) for key in set(source) | set(target) if source.get(key) != target.get(key)})
        if differing:
            raise CodexControlError(tr("Account instructions or settings differ: {items}",
                                       items=", ".join(differing[:4]) + (" …" if len(differing) > 4 else "")))
        self._accept_gaps(identifier, account, self._check_loaded_capabilities(account, env) + self._check_mcp_logins(account, env))
        self._target = account

    def _check_rules(self, identifier: str, current: dict, account: dict) -> None:
        """Different approval rules stop the switch with their names; no flow can allow it. The target is aligned to the
        current rules only after the user confirmed that, and never while another terminal uses the target."""
        source, target = profile_dir(self.root, current), profile_dir(self.root, account)
        differing = codex_rule_differences(source, target)
        if not differing:
            return
        name = str(account.get("name") or identifier)[:64]
        if identifier not in self._align_rules:
            raise CodexHandoffBlocked(tr("Approval rules differ on {account}: {names}", account=name,
                                         names=", ".join(differing[:4]) + (" …" if len(differing) > 4 else "")),
                                      [{"kind": "rules", "name": item[:64]} for item in differing[:16]])
        self._align_rules.discard(identifier)       # one confirmation aligns once
        if account_in_use(self.root, identifier, self.request["session_id"]):
            raise CodexControlError(tr("{account} is open in another terminal; close it there before aligning its approval rules",
                                       account=name))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        align_codex_rules(source, target, self.root / "rules-backups" / safe_id(identifier) / stamp)

    def _accept_gaps(self, identifier: str, account: dict, gaps: list) -> None:
        """Account-level differences go to the user: stop with the list unless they allowed this switch, or the plan
        accepted exactly these for this account (anything that appeared since is not accepted)."""
        accepted = set(((self.policy.accepted_gaps if self.policy else None) or {}).get(identifier, ()))
        if gaps and identifier not in self._allow_gaps and not {gap["kind"] + ":" + gap["name"] for gap in gaps} <= accepted:
            names = ", ".join(gap["name"] for gap in gaps[:6]) + (" …" if len(gaps) > 6 else "")
            raise CodexHandoffBlocked(tr("{account} lacks {names}; install or sign in there, or switch without them",
                                         account=str(account.get("name") or identifier)[:64], names=names), gaps)
        self._gaps_accepted = gaps

    def _check_loaded_capabilities(self, account: dict, env: dict) -> list:
        """What the conversation can use must still be there: every skill Codex loads for this project, with the same
        content, and every connected app (those are linked to the ChatGPT account, like MCP sign-ins). Extra ones on
        the target are allowed."""
        cwd = self.request["path"]
        calls = [("skills/list", {"cwds": [cwd]}), ("plugin/installed", {"cwds": [cwd]}), ("app/installed", {})]
        try:
            source_skills = _skill_inventory(self.bridge.request(*calls[0]))
            source_plugins = _enabled_plugins(self.bridge.request(*calls[1]))
            source_apps = _callable_apps(self.bridge.request("app/installed", {"threadId": self.bridge.snapshot()["thread_id"]}))
            target_skills, target_plugins, target_apps = rpc_requests([trusted_executable("codex"), "app-server"], env, calls,
                                                                      profile_dir(self.root, account), timeout=60)
        except (CodexControlError, RuntimeError):
            raise CodexControlError(tr("Skills and connected apps cannot be verified with this Codex version")) from None
        target_skills, target_apps = _skill_inventory(target_skills), _callable_apps(target_apps)
        target_plugins = _enabled_plugins(target_plugins)
        # Account-level: the user installs, connects or signs in on the target, or accepts switching without them.
        plugins = sorted(identifier.split("@")[0] for identifier, version in source_plugins.items()
                         if target_plugins.get(identifier) != version)
        missing = sorted(key[0] for key, value in source_skills.items() if target_skills.get(key) != value)
        if missing:
            raise CodexControlError(tr("Skills differ on the target account: {names}", names=", ".join(missing[:4])))
        absent = sorted(name for identifier, name in source_apps.items() if identifier not in target_apps)
        return ([{"kind": "plugin", "name": name[:64]} for name in plugins[:16]]
                + [{"kind": "app", "name": str(name)[:64]} for name in absent[:16]])

    def _check_mcp_logins(self, account: dict, env: dict) -> list:
        """MCP OAuth logins live in each profile and are never copied: the target must report its own sign-in for every
        server the conversation uses with OAuth. Credentials are never read; Codex reports the status."""
        try:
            listed = self.bridge.request("mcpServerStatus/list", {"threadId": self.bridge.snapshot()["thread_id"],
                                                                  "detail": "toolsAndAuthOnly", "limit": 100})
        except CodexControlError:
            raise CodexControlError(tr("MCP sign-in cannot be verified with this Codex version")) from None
        servers = listed.get("data")
        if not isinstance(servers, list) or listed.get("nextCursor"):
            raise CodexControlError(tr("MCP sign-in cannot be verified"))
        gaps = []
        for server in servers:
            name = server.get("name") if isinstance(server, dict) else None
            status = server.get("authStatus") if isinstance(server, dict) else None
            if not isinstance(name, str) or status not in {"unknown", "unsupported", "notLoggedIn", "bearerToken", "oAuth"}:
                raise CodexControlError(tr("MCP sign-in status is unknown; switch manually after checking it"))
            if status == "unknown":
                # Codex itself cannot tell whether this server is signed in: the user decides, like a missing sign-in.
                gaps.append({"kind": "mcp_unknown", "name": name[:64]})
                continue
            if status != "oAuth":
                continue            # no per-profile login: the same configuration behaves the same for both accounts
            try:
                found = rpc_request([trusted_executable("codex"), "app-server"], env, "mcpServerStatus/list",
                                    {"serverName": name, "detail": "toolsAndAuthOnly"}, profile_dir(self.root, account))
            except RuntimeError:
                found = {}
            entries = [item for item in found.get("data") or [] if isinstance(item, dict) and item.get("name") == name]
            if not entries or entries[0].get("authStatus") != "oAuth":
                gaps.append({"kind": "mcp", "name": name[:64]})
        return gaps

    def settings_equivalent(self, source: dict, target: dict) -> bool:
        current = self.accounts.get(self.bridge.snapshot()["account"])
        if not current or not self._target:
            return False
        return instruction_sources_match(source.get("instructionSources"), target.get("instructionSources"),
                                         profile_dir(self.root, current), profile_dir(self.root, self._target))

    def command(self, command: dict) -> dict:
        if not isinstance(command, dict):
            raise ValueError("Invalid control command")
        validate_codex_control(dict(command, action="codex-control"))
        if (command.get("environment_id") or "local") != self.environment or command["session_id"] != self.request["session_id"]:
            raise ValueError("Control command belongs to another terminal")
        if any(account.get("tool") != self.request["account"]["tool"] for account in command.get("accounts", [])):
            raise ValueError("Switch between accounts of the same CLI")
        action = command["control_action"]
        if action == "policy":
            self.apply_policy(command.get("policy"), command.get("accounts", []))
        elif action == "switch":
            for account in command.get("accounts", []):
                self.accounts[account["id"]] = dict(self.accounts.get(account["id"], {}), **account)
            if command["target_account_id"] not in self.accounts:
                raise ValueError("Select a target account in this environment")
            if command.get("allow_gaps") is True:
                self._allow_gaps.add(command["target_account_id"])
            if command.get("align_rules") is True:
                self._align_rules.add(command["target_account_id"])
            self.waiting, self.next_retry, self.notice = False, None, ""
            self.bridge.request_switch(command["target_account_id"])
            self._automatic = None          # the user's choice: no flow change withdraws it
        elif action == "cancel":
            # Cancelling also stops automatic switching; otherwise the next check would queue the same switch again.
            self.bridge.cancel_switch()
            self._allow_gaps.clear()
            self.apply_policy(None, [])
        elif action == "resume":
            self.bridge.resume_work()
        return self.status()

    def _commands(self) -> None:
        for path in sorted((self.folder / "commands").glob("*.json"))[:32]:
            if path.name in self._handled:
                continue
            self._handled.add(path.name)
            try:
                if not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
                    raise ValueError("Unexpected control file")
                command = json.loads(read_private(path, limit=65536))
                if not isinstance(command, dict) or command.get("id") != path.stem:
                    raise ValueError("Control command identifier mismatch")
                created = command.get("created")
                if type(created) not in (int, float) or not 0 <= time.time() - created <= CONTROL_COMMAND_MAX_AGE:
                    raise ValueError("Control command expired")
                reply = dict(self.command(command), accepted=True)
            except Exception:
                # Never echo the request or an exception: either may carry details the reply must not.
                reply = dict(self.status(), accepted=False,
                             message=tr("Control request rejected; verify its account pool and settings."))
            try:
                write_json(self.folder / "replies" / path.name, reply)
            finally:
                path.unlink(missing_ok=True)
        cutoff = time.time() - CONTROL_COMMAND_MAX_AGE
        for path in list((self.folder / "replies").glob("*.json"))[:64]:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)       # the asking side gave up long ago
            except OSError:
                pass

    def _evaluate(self, before: dict) -> None:
        policy = self.policy
        if (not policy or not self._attached or before["state"] in {"needs_attention", "switching", "closed"}
                or before["pending_account"]):
            return
        moment = time.time()
        # A failure that was handed off or continued is over; a later one has its own time.
        failed = before.get("last_error_kind") in QUOTA_ERRORS and before.get("last_error_at") != self._settled
        if self.PASSIVE_QUOTA and self.waiting and (failed and before["state"] == "running" or self.wait_reset is not None
                                                    and moment >= self.wait_reset + self.CONTINUE_GRACE):
            self._claude_after_limit(before, failed)
            return
        current = before["account"]
        snapshots = self.monitor.snapshots()
        cached = snapshots.get(current)
        observed = observed_quota(cached, before, self.environment)
        for identifier, fake in self._simulated_quota(moment).items():
            # Test only: the simulated observation replaces the real one and then takes the normal path.
            if identifier == current:
                cached = observed = fake
            else:
                snapshots[identifier] = fake
        if failed:
            # A turn that failed on quota is a known exhaustion until a later full query says otherwise; a status line
            # (passive quota) is no query, so it stays one until the task continues.
            failed_at = before.get("last_error_at") or moment
            checked = _quota_timestamp((observed or {}).get("checked_at"))
            if self.PASSIVE_QUOTA or checked is None or checked < failed_at:
                buckets = (observed or {}).get("buckets")
                meter = (buckets.get("codex") if isinstance(buckets, dict) else observed) or {}
                observed = {"tool": "codex", "account_id": current, "environment_id": self.environment,
                            "checked_at": moment if self.PASSIVE_QUOTA else failed_at,
                            "buckets": {"codex": {"windows": meter.get("windows") or [], "reached": before["last_error_kind"]}}}
        remaining, reset = quota_state(observed or {}, self.environment, moment, policy.interval, policy.threshold)
        if remaining is None:
            return                  # unknown quota is never a reason to move the conversation
        if remaining > policy.threshold and self.waiting:
            self._recovered(before, cached, moment, failed)
            return
        blocked = before.get("blocked") or {}
        if blocked.get("target") and blocked.get("at") != self._blocked_seen:
            # Each report counts once: try other accounts first and ask again later (a changed flow decides afresh).
            self._blocked_seen = blocked.get("at")
            self._skip[blocked["target"]] = moment + policy.interval
        others = {key: value for key, value in snapshots.items() if key != current and self._skip.get(key, 0) <= moment}
        retries = []

        def choose(pool: tuple, strategy: str) -> str | None:
            selection = select_quota_account(replace(policy, account_ids=pool, strategy=strategy, when_empty="wait"), self.accounts, others,
                                             self.environment, moment, cursor=self.cursor)
            if selection.next_retry is not None:
                retries.append(selection.next_retry)
            return selection.account_id if selection.account_id != current else None

        facts = {"low": lambda: remaining <= policy.threshold, "exhausted": lambda: remaining <= 0,
                 "available": lambda: choose(policy.account_ids, "priority") is not None}
        outcome, target = flow_outcome(quota_flow(policy), lambda test: facts[test](), choose)
        if outcome == "handoff":
            self._follow_attachment(fresh=True)
            if not self._attached:
                return
            self.waiting, self.next_retry, self.notice = False, None, ""
            self._automatic = (target, self._version)
            self.bridge.request_switch(target)
            return
        if outcome == "keep":
            return
        self._wait_mode = outcome
        # Only the current account's own trusted reset can later continue the task on it (see _recovered); the
        # earliest reset in the pool is when another account may become selectable again.
        reset = reset if outcome == "wait" else None
        pool_retry = min(retries) if retries and outcome == "wait" else None
        if reset is not None or not self.waiting or outcome != "wait":
            # Right after a trusted reset the old observation still says "exhausted, reset passed" until the next
            # query: that must not erase the reset the recovery is waiting for.
            self.wait_reset = reset
        waits = [value for value in (pool_retry, self.wait_reset) if value is not None]
        self.waiting, self.next_retry = True, min(waits) if waits else None
        if self.wait_reset is not None:
            self.monitor.require_after({current}, self.wait_reset)
        if pool_retry is not None:
            self.monitor.require_after(set(policy.account_ids), pool_retry)

    def _simulation(self) -> dict | None:
        """``simulated-quota.json`` of this terminal, written only by scripts/simulate_quota.py; at most one hour old."""
        path = self.folder / "simulated-quota.json"
        try:
            data = json.loads(read_private(path, limit=65536))
            written = path.stat().st_mtime
        except (OSError, ValueError):
            return None
        expires = data.get("expires_at") if isinstance(data, dict) else None
        if type(expires) not in (int, float) or not time.time() < expires <= written + 3600:
            return None
        return data

    def _simulated_quota(self, moment: float) -> dict:
        simulation = self._simulation()
        self.simulated = simulation is not None
        result = {}
        for identifier, fake in ((simulation or {}).get("accounts") or {}).items():
            if not isinstance(fake, dict) or identifier not in self.accounts:
                continue
            windows, after = fake.get("windows"), fake.get("after")
            if isinstance(after, dict) and type(after.get("at")) in (int, float) and moment >= after["at"]:
                windows = after.get("windows")
            result[identifier] = {"tool": self.accounts[identifier].get("tool"), "account_id": identifier, "simulated": True,
                                  "environment_id": self.environment, "checked_at": moment,
                                  "windows": windows if isinstance(windows, list) else [], "reached": None}
        return result

    def _simulated_failure(self) -> None:
        """Test only: once, treat the last completed turn as stopped by quota, so the real handoff and continuation run."""
        simulation = self._simulation()
        self.simulated = simulation is not None
        if not simulation or simulation.get("quota_failure_once") is not True:
            return
        if self.bridge.simulate_quota_failure():
            write_json(self.folder / "simulated-quota.json", dict(simulation, quota_failure_once="used"))

    def _recovered(self, before: dict, cached: dict | None, moment: float, failed: bool) -> None:
        """Leave the waiting state; continue the failed task only after a trusted reset passed, a query made after it
        shows quota again, the failure was a known quota failure and the bridge reports a verified safe checkpoint."""
        policy, reset = self.policy, self.wait_reset
        checked = _quota_timestamp((cached or {}).get("checked_at"))
        remaining = quota_state(cached or {}, self.environment, moment, policy.interval, policy.threshold)[0]
        verified = (self._wait_mode == "wait" and failed and reset is not None
                    and moment >= reset and checked is not None and checked >= reset
                    and remaining is not None and remaining > policy.threshold)
        if verified and not before.get("can_switch") and before["state"] != "running":
            return                  # wait for the safe checkpoint before deciding
        self.waiting, self.next_retry, self.wait_reset = False, None, None
        self.notice = tr("Quota is available again; continue the task in the terminal when ready.")
        if verified and before.get("can_switch"):
            try:
                self.bridge.resume_work(recovered=True)
                self.notice = tr("Quota recovered after its reset; the unfinished task continues.")
                self._settled = before.get("last_error_at")
            except CodexControlError:
                pass
        elif before["state"] == "running":
            self.notice = ""

    def _claude_after_limit(self, before: dict, failed: bool) -> None:
        """Claude Code continues a task stopped by a usage limit by itself after the reset ("Continue automatically at
        usage limit", on by default). The launcher never restarts it for that: work running again ends the wait; a
        task still stopped well after the reset (setting off, wait cancelled, computer asleep) is yours to continue."""
        if before["state"] == "running":
            self._settled = before.get("last_error_at")
            self.waiting, self.next_retry, self.wait_reset, self.notice = False, None, None, ""
        else:
            self.next_retry = self.wait_reset = None
            self.waiting = failed

    def tick(self) -> dict:
        self._commands()
        self._follow_attachment()
        self._simulated_failure()
        before = self.bridge.snapshot()
        self._evaluate(before)
        after = self.bridge.tick()
        if after["account"] != before["account"]:
            # The old account's quota failure is over: its continuation (below) belongs to the handoff.
            self.notice, self._settled = "", after.get("last_error_at")
            if self._gaps_accepted:
                self.notice = tr("Switched without {names}", names=", ".join(gap["name"] for gap in self._gaps_accepted[:6]))
            self._allow_gaps.discard(after["account"])
            self._gaps_accepted = []
            if self.policy and after["account"] in self.policy.account_ids:
                self.cursor = commit_quota_cursor(self.policy, after["account"])
            # A manual switch, or an automatic one of the flow still applied, continues a task that failed on quota.
            automatic, self._automatic = self._automatic, None
            current = automatic is None or (automatic[0] == after["account"] and automatic[1] == self._version)
            resumes = not self.policy or any(node["type"] == "wait" for node in quota_flow(self.policy)["nodes"])
            self._continue = after["last_error_kind"] in QUOTA_ERRORS and current and resumes and not self._stale_handoff
            self._stale_handoff = False
        if self._continue:
            # The task that failed on quota continues on the new account at the first safe moment; the bridge drops
            # the checkpoint if that moment does not come soon.
            if after.get("continuation_ready") is False:
                self._continue = False
            else:
                try:
                    self.bridge.resume_work()
                    self._continue = False
                except CodexControlError:
                    pass
        return self._publish()

    def _publish(self) -> dict:
        result = self.status()
        if result != self._last:
            write_json(self.folder / "state.json", result)
            self._last = result
        return result

    def disable(self) -> None:
        """An internal error stops automatic switching; the terminal and its conversation keep running."""
        try:
            self._cancel_automatic()
        except Exception:
            pass
        self.policy, self.waiting, self.next_retry = None, False, None
        self.notice = tr("Automatic account switching stopped after an internal error; manual switching still works.")
        self.monitor.configure(None, self.accounts)

    def close(self) -> None:
        self._closed.set()
        clean = True
        for step in (self.monitor.close, self.bridge.close):
            try:
                step()
            except Exception:
                clean = False
        result = dict(self.status(), status="closed", can_switch=False, pending_account=None)
        if not clean:
            result.update(status="needs_attention", message=tr(
                "The previous Codex app-server did not exit cleanly; check for a leftover codex process before reopening."))
        try:
            write_json(self.folder / "state.json", result)
        except OSError:
            pass


def thread_owner_active(root: Path, thread_id: str, session_id: str) -> bool:
    """Is this exact conversation still owned by another live managed worker on this host?"""
    for folder in list((root / "codex-controls").glob("*"))[:4096]:
        if folder.name == session_id:
            continue
        try:
            record = json.loads(read_private(folder / "state.json", limit=65536))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("thread_id") != thread_id:
            continue
        if record.get("status") not in {"closed", "unmanaged"} and control_worker_alive(folder, record):
            return True
        # A worker that died abruptly may leave its app-server finishing a write; wait for that writer too.
        age = control_heartbeat_age(folder)
        if (record.get("status") != "closed" and age is not None and age < 600
                and process_alive(record.get("backend_pid")) is True):
            return True
    return False


def stop_process(process) -> None:
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        process.terminate()


def run_managed_codex(request: dict, root: Path, env: dict, stop_path: Path) -> int | None:
    """Run the native TUI against a launcher-owned app-server bridge. ``None``: this CLI cannot, run it unmanaged."""
    try:
        executable = trusted_executable("codex")
    except RuntimeError:
        return None                     # the unmanaged start reports the missing CLI as it always has
    folder = codex_control_path(root, request["session_id"])
    if not managed_codex_supported(executable, env, root):
        write_json(folder / "state.json", {"status": "unmanaged", "environment_id": request.get("environment_id") or "local",
                                           "account_id": request["account"]["id"], "message":
                                           tr("This Codex CLI cannot run safe account management; update Codex to enable switching.")})
        print(tr("This Codex CLI cannot hand the conversation to another account; it starts without account management."), flush=True)
        return None
    thread_id = request.get("resume_thread_id")
    if thread_id:
        deadline = time.monotonic() + 20
        if thread_owner_active(root, thread_id, request["session_id"]):
            print(tr("Waiting for the earlier terminal of this conversation to close..."), flush=True)
        while thread_owner_active(root, thread_id, request["session_id"]):
            if time.monotonic() > deadline:
                raise RuntimeError(tr("This conversation is still open in another terminal; close it there first."))
            time.sleep(0.5)
    handlers = {}
    if not WINDOWS and threading.current_thread() is threading.main_thread():
        # Closing the tmux session or stopping the worker must still shut the app-server down through ``finally``.
        for number, code in ((signal.SIGTERM, 143), (signal.SIGHUP, 129)):
            handlers[number] = signal.signal(number, lambda *_args, code=code: sys.exit(code))
    bridge = controller = process = None
    try:
        def factory(identifier, settings):
            account = controller.accounts[identifier] if controller else request["account"]
            return subprocess.Popen(managed_codex_command(account, root, executable, settings),
                                    cwd=request["path"], env=prepare(account, root), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    creationflags=0x08000000 if WINDOWS else 0, start_new_session=not WINDOWS)

        bridge = ManagedCodexBridge(factory, request["account"]["id"])
        controller = ManagedCodexSession(request, root, bridge)
        bridge._validate_target = controller.validate_target
        bridge._settings_equivalent = controller.settings_equivalent
        endpoint, token = bridge.start()
        command = [executable, "--remote", endpoint, "--remote-auth-token-env", "VIBE_CODEX_CONTROL_TOKEN"]
        if thread_id:
            command += ["resume", thread_id]
        elif request.get("resume") is True and resumable(request["account"], root, Path(request["path"])):
            command += ["resume", "--last"]
        process = subprocess.Popen(command, cwd=request["path"], env=dict(env, VIBE_CODEX_CONTROL_TOKEN=token))
        save_status(root, request, "running", pid=process.pid)
        stopping = False
        while process.poll() is None:
            try:
                controller.tick()
            except Exception:
                controller.disable()
            if stop_path.exists() and not stopping:
                stopping = True
                stop_process(process)
            try:
                time.sleep(0.3)
            except KeyboardInterrupt:
                continue            # Ctrl+C belongs to the TUI; it may cancel only the current answer
        result = process.wait()
        save_status(root, request, "ended" if result == 0 or result in USER_QUIT_CODES else "failed")
        return result
    finally:
        if process is not None and process.poll() is None:
            stop_process(process)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if controller is not None:
            controller.close()
        elif bridge is not None:
            try:
                bridge.close()
            except Exception:
                pass
        for number, handler in handlers.items():
            signal.signal(number, handler)
        stop_path.unlink(missing_ok=True)


# One JSON-RPC message. Real answers are large (``plugin/list`` with marketplace data measured 13 MB); this matches the
# native TUI's WebSocket message limit. Larger output still fails closed.
_BRIDGE_LIMIT = 64 * 1024 * 1024
_BRIDGE_FRAME = 4 * 1024 * 1024     # outgoing fragments stay far below the client's per-frame limit
_BRIDGE_SETTINGS = (
    "model", "modelProvider", "reasoningEffort", "cwd", "approvalPolicy",
    "approvalsReviewer", "sandbox", "instructionSources", "disabledPluginIds",
    "serviceTier", "collaborationMode",
)


class CodexControlError(RuntimeError):
    """A sanitized, actionable control failure."""


class CodexHandoffBlocked(CodexControlError):
    """The target account lacks account-level capabilities the source has. Nothing was changed; the user decides:
    install or sign in on the target, pick another account, or switch without them."""
    def __init__(self, message: str, gaps: list):
        super().__init__(message)
        self.gaps = gaps


class ManagedCodexBridge:
    def __init__(self, backend_factory: Callable[[str, dict[str, Any]], Any],
                 initial_account: str, *, deadline: float = 15.0,
                 validate_target: Callable[[str, dict[str, Any]], None] | None = None,
                 settings_equivalent: Callable[[dict[str, Any], dict[str, Any]], bool] | None = None):
        self._factory = backend_factory
        self._account = initial_account
        self._deadline = deadline
        self._validate_target = validate_target
        self._settings_equivalent = settings_equivalent
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._front_write = threading.Lock()
        self._process: Any = None
        self._listener: socket.socket | None = None
        self._frontend: socket.socket | None = None
        self._capability = secrets.token_urlsafe(32)
        self._closed = False
        self._generation = 0
        self._state = "starting"
        self._reason = ""
        self._thread_id: str | None = None
        self._settings: dict[str, Any] = {}
        self._init: dict[str, Any] | None = None
        self._pending: dict[Any, tuple[str, dict[str, Any], float]] = {}
        self._approvals: set[Any] = set()
        self._tools: set[str] = set()
        self._internal: dict[str, queue.Queue] = {}
        self._pending_account: str | None = None
        self._child_unknown = False
        self._switching = False
        self._last_turn_status: str | None = None
        self._last_error_kind: str | None = None
        self._continuation_ready = False
        self._continuation_until = 0.0
        self._settings_unknown = False
        # Live values a resume answer does not report. Personality and reasoning summary are requested explicitly on
        # the target and checked when it next reports its settings; a permission profile cannot be requested there.
        self._extra: dict[str, Any] = {}
        self._verify: dict[str, Any] = {}
        self._blocked: dict[str, Any] | None = None
        self._rate_limits: dict[str, Any] | None = None
        self._rate_limit_times: dict[str, float] = {}
        self._rate_limits_checked_at: float | None = None
        self._last_error_at: float | None = None
        self._turn_id: str | None = None
        self._client_slots = threading.BoundedSemaphore(4)
        # The native TUI's own tool-free helper threads (its title generator): never the conversation, never a writer.
        self._side_requests: set[Any] = set()
        self._side_threads: set[str] = set()
        self._side_turns: set[str] = set()

    def start(self) -> tuple[str, str]:
        with self._lock:
            if self._listener is not None or self._closed:
                raise CodexControlError("Bridge already started or closed")
            self._spawn(self._account, {})
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(2)
            listener.settimeout(0.5)
            self._listener = listener
            threading.Thread(target=self._accept, daemon=True).start()
            self._state = "unknown"
            return f"ws://127.0.0.1:{listener.getsockname()[1]}/", self._capability

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"state": self._state, "account": self._account,
                    "thread_id": self._thread_id, "pending_account": self._pending_account,
                    "reason": self._reason, "active_tools": len(self._tools),
                    "pending_approvals": len(self._approvals),
                    "last_turn_status": self._last_turn_status,
                    "last_error_kind": self._last_error_kind,
                    "last_error_at": self._last_error_at,
                    "tool_outcomes_known": not self._tools and not self._child_unknown,
                    "rate_limits": copy.deepcopy(self._rate_limits),
                    "rate_limits_checked_at": self._rate_limits_checked_at,
                    "rate_limit_times": dict(self._rate_limit_times),
                    "backend_pid": getattr(self._process, "pid", None),
                    "continuation_ready": self._continuation_ready and time.monotonic() < self._continuation_until,
                    "blocked": copy.deepcopy(self._blocked),
                    "can_switch": self._safe() and not self._verify}

    def request_switch(self, target_account: str) -> dict[str, Any]:
        with self._lock:
            if not target_account or self._closed:
                raise CodexControlError("Invalid switch request")
            if target_account == self._account:
                return self.snapshot()
            if self._pending_account and self._pending_account != target_account:
                raise CodexControlError(tr("Another account switch is pending"))
            self._pending_account = target_account
            self._blocked = None
        return self.snapshot()

    def cancel_switch(self) -> dict[str, Any]:
        with self._lock:
            if self._switching:
                raise CodexControlError(tr("Handoff is already in progress"))
            self._pending_account = None
            if self._state == "needs_attention" and self._process is not None and self._process.poll() is None:
                # Preserve the original writer without asserting unverified quiescence.
                self._state = "unknown"
                self._reason = tr("Switch cancelled; original account remains active")
        return self.snapshot()

    def _safe(self) -> bool:
        return bool(self._thread_id and self._init is not None and self._settings
                    and self._state == "idle" and not self._pending and not self._tools
                    and not self._approvals and not self._child_unknown and not self._switching
                    and not self._settings_unknown and self._turn_id is None and not self._side_turns)

    def tick(self) -> dict[str, Any]:
        with self._lock:
            if any(time.monotonic() - entry[2] > self._deadline for entry in self._pending.values()):
                self._attention(tr("Native request timed out; inspect the terminal before switching"))
            if not self._pending_account or not self._safe() or self._verify:
                return self.snapshot()
            target = self._pending_account
            self._switching = True
            self._state = "switching"
            thread_id = self._thread_id
        source_stopped = False
        try:
            # Exact persisted history plus authoritative live settings, never latest-thread lookup.
            read = self._rpc("thread/read", {"threadId": thread_id, "includeTurns": True})
            thread = read.get("thread", {})
            if thread.get("id") != thread_id or not self._quiescent(thread):
                raise CodexControlError(tr("Source thread is not verified idle"))
            if not thread.get("turns"):
                raise CodexControlError(tr("Thread has no persisted turn; finish a turn before switching"))
            source = self._rpc("thread/resume", {"threadId": thread_id})
            source_settings = self._settings_from(source)
            if (source.get("thread", {}).get("id") != thread_id
                    or not self._quiescent(source.get("thread", {}))
                    or source_settings != self._settings):
                raise CodexControlError(tr("Source settings changed; inspect the terminal before switching"))
            if self._validate_target:
                self._validate_target(target, copy.deepcopy(source_settings))
            with self._lock:
                if self._tools or self._approvals or self._pending or self._child_unknown:
                    raise CodexControlError(tr("Execution became uncertain during handoff"))
            with self._lock:
                source_account = self._account
                carried = {key: copy.deepcopy(self._extra[key]) for key in ("personality", "summary") if key in self._extra}
            self._stop_backend()
            source_stopped = True
            resumed = self._resume_on(target, thread_id, source_settings, carried)
            if not self._equivalent(source_settings, self._settings_from(resumed)):
                raise CodexControlError(tr("Target settings differ; automatic continuation stopped"))
            with self._lock:
                # The new owner's own view is the baseline from now on (its instruction files have its paths).
                self._settings = self._settings_from(resumed)
                self._account = target
                self._rate_limits = None
                self._rate_limit_times = {}
                self._rate_limits_checked_at = None
                self._pending_account = None
                self._state = "idle"
                self._reason = ""
                self._side_threads.clear()      # they lived only in the previous owner
                self._blocked = None
                self._verify = carried              # confirmed when the new owner next reports its settings
                self._continuation_ready = self._last_error_kind in QUOTA_ERRORS
                # The checkpoint is only good right after the handoff; later the user decides.
                self._continuation_until = time.monotonic() + 120
        except CodexHandoffBlocked as error:
            with self._lock:
                # Nothing was stopped or changed: the source stays the verified owner and the user decides.
                self._pending_account = None
                self._blocked = {"target": target, "gaps": copy.deepcopy(error.gaps), "at": time.time()}
                self._reason = str(error)
                if self._state == "switching" and not (self._tools or self._approvals or self._pending or self._child_unknown):
                    self._state = "idle"
                elif self._state == "switching":
                    self._attention(str(error))
        except Exception as error:
            with self._lock:
                # Before the source stopped, our own preflight reason tells the user what to fix; it holds no request data.
                self._attention(str(error) if isinstance(error, CodexControlError) and not source_stopped
                                else tr("Account handoff could not be verified; reopen this exact thread manually"))
            # Do not permit execution on an unverified replacement: stop it, then bring the source account back.
            if source_stopped:
                self._stop_backend()
                self._restore_source(source_account, thread_id, source_settings, carried)
        finally:
            with self._lock:
                self._switching = False
        return self.snapshot()

    def _resume_on(self, account: str, thread_id: str, settings: dict[str, Any], carried: dict[str, Any]) -> dict[str, Any]:
        """Start ``account``'s app-server and resume the exact thread there with the conversation's effective settings."""
        self._spawn(account, copy.deepcopy(settings))
        self._rpc("initialize", copy.deepcopy(self._init or {}))
        self._write({"method": "initialized"})
        params = {"threadId": thread_id}
        for key in ("model", "modelProvider", "cwd", "approvalPolicy", "approvalsReviewer", "serviceTier"):
            # An unset value stays unset: an explicit null is answered with the server's default name instead.
            if settings.get(key) is not None:
                params[key] = settings[key]
        effort = settings.get("reasoningEffort")
        if effort is not None:
            params["config"] = {"model_reasoning_effort": effort}
        if carried.get("personality") is not None:
            params["personality"] = carried["personality"]
        if carried.get("summary") is not None:
            params.setdefault("config", {})["model_reasoning_summary"] = carried["summary"]
        sandbox = settings.get("sandbox", {})
        sandbox_type = sandbox.get("type") if isinstance(sandbox, dict) else None
        if sandbox_type in ("readOnly", "workspaceWrite", "dangerFullAccess"):
            params["sandbox"] = {"readOnly": "read-only", "workspaceWrite": "workspace-write",
                                 "dangerFullAccess": "danger-full-access"}[sandbox_type]
        resumed = self._rpc("thread/resume", params)
        if resumed.get("thread", {}).get("id") != thread_id:
            raise CodexControlError(tr("Target resumed a different thread"))
        if not self._quiescent(resumed.get("thread", {})):
            raise CodexControlError(tr("Target thread is not verified idle"))
        return resumed

    def _restore_source(self, account: str, thread_id: str, settings: dict[str, Any], carried: dict[str, Any]) -> None:
        """After a failed handoff the conversation returns to its own account with the same settings; the reported
        account never changed. If that cannot be verified either, nothing runs and the thread is the checkpoint."""
        try:
            resumed = self._resume_on(account, thread_id, settings, carried)
            if self._settings_from(resumed) != settings:
                raise CodexControlError("Source settings changed")
        except Exception:
            try:
                self._stop_backend()
            except Exception:
                pass
            with self._lock:
                self._attention(tr("Account handoff failed and the original account could not be restored; "
                                   "reopen this exact thread manually"))
            return
        with self._lock:
            self._pending_account = None
            self._state = "idle"
            self._reason = tr("The switch to the target account failed; the original account was restored.")

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """A launcher-owned request to the current owner; its reply never reaches the terminal."""
        return self._rpc(method, params)

    def simulate_quota_failure(self) -> bool:
        """Test only (scripts/simulate_quota.py): mark the last completed turn as stopped by quota."""
        with self._lock:
            if self._state != "idle" or self._last_turn_status != "completed" or self._turn_id is not None:
                return False
            self._last_turn_status, self._last_error_kind, self._last_error_at = "failed", "usageLimitExceeded", time.time()
            return True

    def resume_work(self, recovered: bool = False) -> dict[str, Any]:
        """Continue only a verified quota failure: after a successful safe handoff, or (``recovered``) once the caller
        verified that the same account's quota came back after its reset."""
        with self._lock:
            ready = ((self._continuation_ready and time.monotonic() < self._continuation_until)
                     or (recovered and self._last_error_kind in QUOTA_ERRORS))
            if not ready or not self._safe():
                raise CodexControlError("No verified quota checkpoint is available for continuation")
            self._continuation_ready = False
            thread_id = self._thread_id
        self._frontend_message({"id": "vibe-continue-" + secrets.token_hex(16),
            "method": "turn/start", "params": {"threadId": thread_id,
                "input": [{"type": "text", "text": "Continue the unfinished task from its existing checkpoint. Review completed tool actions; do not repeat them."}],
                "clientUserMessageId": "vibe-handoff-" + secrets.token_hex(16)}})
        return self.snapshot()

    def _quiescent(self, thread: dict[str, Any]) -> bool:
        """Codex reports idle, or systemError after a turn that failed; the latter is a checkpoint only when that turn's
        own completion said it failed on quota (nothing else runs then: the caller also checked turns, tools, approvals)."""
        status = (thread.get("status") or {}).get("type") if isinstance(thread, dict) else None
        return status == "idle" or (status == "systemError" and self._last_turn_status == "failed"
                                    and self._last_error_kind in QUOTA_ERRORS)

    def _attention(self, reason: str) -> None:
        self._state = "needs_attention"
        self._reason = reason

    @staticmethod
    def _settings_from(result: dict[str, Any]) -> dict[str, Any]:
        return {key: copy.deepcopy(result[key]) for key in _BRIDGE_SETTINGS if key in result}

    def _equivalent(self, source: dict[str, Any], target: dict[str, Any]) -> bool:
        # Caller may normalize verified instruction file locations only. Permissions,
        # model and all other effective values can never be relaxed by a callback.
        if {k: v for k, v in source.items() if k != "instructionSources"} != {
                k: v for k, v in target.items() if k != "instructionSources"}:
            return False
        if source == target:
            return True
        return bool(self._settings_equivalent and self._settings_equivalent(source, target))

    def _spawn(self, account: str, settings: dict[str, Any]) -> None:
        process = self._factory(account, settings)
        if process.stdin is None or process.stdout is None:
            raise CodexControlError("Backend must provide stdio pipes")
        with self._lock:
            self._process = process
            self._generation += 1
            generation = self._generation
        threading.Thread(target=self._read_backend, args=(process, generation), daemon=True).start()

    def _write(self, message: dict[str, Any]) -> None:
        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        if len(payload) > _BRIDGE_LIMIT:
            raise CodexControlError("Native message exceeds size limit")
        with self._write_lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise CodexControlError("Native backend is unavailable")
            process.stdin.write(payload)
            process.stdin.flush()

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        identifier = "vibe-control-" + secrets.token_hex(16)
        replies: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._internal[identifier] = replies
        try:
            self._write({"id": identifier, "method": method, "params": params})
            try:
                reply = replies.get(timeout=self._deadline)
            except queue.Empty:
                raise CodexControlError("Native control request timed out") from None
            if "error" in reply or not isinstance(reply.get("result"), dict):
                raise CodexControlError("Native control request was rejected")
            return reply["result"]
        finally:
            with self._lock:
                self._internal.pop(identifier, None)

    def _read_backend(self, process: Any, generation: int) -> None:
        try:
            while True:
                line = process.stdout.readline(_BRIDGE_LIMIT + 1)
                if not line:
                    break
                if len(line) > _BRIDGE_LIMIT:
                    raise CodexControlError("Native output exceeds size limit")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise CodexControlError("Invalid native message")
                with self._lock:
                    if generation != self._generation:
                        return
                    internal = self._internal.get(message.get("id"))
                    if internal is not None and "method" not in message:
                        internal.put_nowait(message)
                        continue
                    self._observe(message)
                self._send_front(message)
        except Exception:
            pass
        with self._lock:
            if generation == self._generation and not self._closed and not self._switching:
                self._attention(tr("Native backend disconnected; execution status is unknown"))

    def _observe(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        identifier = message.get("id")
        if method is None:
            entry = self._pending.pop(identifier, None)
            side = identifier in self._side_requests
            self._side_requests.discard(identifier)
            if entry and "result" in message:
                request_method, _request_params, _ = entry
                result = message["result"]
                if request_method in ("thread/start", "thread/resume"):
                    thread = result.get("thread", {})
                    new_id = thread.get("id")
                    if side and thread.get("ephemeral") is True and isinstance(new_id, str) and new_id != self._thread_id:
                        self._side_threads.add(new_id)
                        return
                    if self._thread_id and self._thread_id != new_id:
                        self._attention(tr("Terminal selected a different conversation"))
                        return
                    self._thread_id = new_id
                    self._settings = self._settings_from(result)
                    self._state = "idle" if thread.get("status", {}).get("type") == "idle" else "unknown"
            return
        if identifier is not None:
            self._approvals.add(identifier)
        event_thread = params.get("threadId")
        if method == "thread/started":
            event_thread = params.get("thread", {}).get("id")
            if (self._side_requests and params.get("thread", {}).get("ephemeral") is True
                    and isinstance(event_thread, str) and event_thread != self._thread_id):
                self._side_threads.add(event_thread)     # announced before the reply to its tool-free request
        if event_thread in self._side_threads:
            self._observe_side(method, params, event_thread)
            return
        if event_thread and self._thread_id and event_thread != self._thread_id:
            self._child_unknown = True
            self._attention(tr("Another thread or child agent requires manual verification"))
            return
        if method == "turn/started":
            turn_id = params.get("turn", {}).get("id")
            if not self._thread_id or event_thread != self._thread_id or not isinstance(turn_id, str) or not turn_id:
                self._attention(tr("Unbound turn requires manual verification"))
                return
            self._turn_id = turn_id
            self._continuation_ready = False
            self._state = "running"
        elif method == "account/rateLimits/updated":
            limits = params.get("rateLimits")
            # A sparse rolling update of the Codex meter: merge what it carries, each value with its own observation
            # time; an absent or null value says nothing (other meters belong to other products).
            if isinstance(limits, dict) and limits.get("limitId") in (None, "codex"):
                stamp = time.time()
                merged = self._rate_limits or {}
                for key, value in limits.items():
                    if value is not None and key in ("limitId", "limitName", "primary", "secondary", "planType", "credits",
                                                     "rateLimitReachedType", "spendControlReached", "individualLimit"):
                        merged[key] = copy.deepcopy(value)
                        self._rate_limit_times[key] = stamp
                self._rate_limits, self._rate_limits_checked_at = merged, stamp
        elif method == "turn/completed":
            turn_id = params.get("turn", {}).get("id")
            if (not self._thread_id or event_thread != self._thread_id
                    or not self._turn_id or turn_id != self._turn_id):
                self._attention(tr("Unmatched turn completion requires manual verification"))
                return
            self._turn_id = None
            status = params.get("turn", {}).get("status")
            self._last_turn_status = status if status in ("completed", "failed", "interrupted") else "unknown"
            error_info = (params.get("turn", {}).get("error") or {}).get("codexErrorInfo")
            kind = error_info if isinstance(error_info, str) else None
            self._last_error_kind = kind if kind in QUOTA_ERRORS else None
            self._last_error_at = time.time() if self._last_error_kind else None
            known = status == "completed" or (status == "failed" and self._last_error_kind is not None)
            self._state = "idle" if known and not self._child_unknown else "needs_attention"
            if known and self._verify:
                # Codex reports settings only when they change; a whole turn on the new owner without a contrary report
                # confirms the values requested at resume.
                self._verify = {}
        elif method == "thread/settings/updated":
            settings = dict(params.get("threadSettings", {}))
            if "effort" in settings:
                settings["reasoningEffort"] = settings.pop("effort")
            if "sandboxPolicy" in settings:
                settings["sandbox"] = settings.pop("sandboxPolicy")
            self._settings.update(self._settings_from(settings))
            for key in ("personality", "summary", "activePermissionProfile"):
                if key in settings:
                    self._extra[key] = copy.deepcopy(settings[key])
            # A permission profile cannot be requested on the target, so a conversation using one is switched by hand.
            self._settings_unknown = self._extra.get("activePermissionProfile") is not None
            if self._verify:
                if any(key in settings and settings[key] != value for key, value in self._verify.items()):
                    self._attention(tr("The new account reports a different personality or reasoning summary; check the terminal"))
                elif all(key in settings for key in self._verify):
                    self._verify = {}
        elif method in ("item/started", "item/completed"):
            item = params.get("item", {})
            item_type = item.get("type", "")
            if item_type.startswith("collab"):
                self._child_unknown = True
            if item_type not in ("userMessage", "agentMessage", "reasoning", "plan"):
                if method == "item/started":
                    self._tools.add(str(item.get("id")))
                elif (item.get("status") in ("completed", "failed", "declined")
                      and (item_type != "commandExecution" or type(item.get("exitCode")) is int)):
                    self._tools.discard(str(item.get("id")))
                else:
                    self._tools.add(str(item.get("id")))

    def _observe_side(self, method: str, params: dict[str, Any], thread: str) -> None:
        if method == "turn/started":
            self._side_turns.add(thread)
        elif method == "turn/completed":
            self._side_turns.discard(thread)
        elif method in ("item/started", "item/completed"):
            if params.get("item", {}).get("type") not in ("userMessage", "agentMessage", "reasoning", "plan"):
                self._child_unknown = True
                self._attention(tr("A helper thread used a tool; inspect the terminal before switching"))

    @staticmethod
    def _tool_free_side_thread(params: dict[str, Any]) -> bool:
        """An ephemeral, read-only thread with every tool switched off: it can only answer in text."""
        config = params.get("config") if isinstance(params.get("config"), dict) else {}
        servers = config.get("mcp_servers")
        # With MCP servers configured, the TUI switches each one off by name instead of sending an empty table.
        no_mcp = isinstance(servers, dict) and all(isinstance(entry, dict) and entry.get("enabled") is False
                                                   for entry in servers.values())
        return (params.get("ephemeral") is True and params.get("sandbox") == "read-only" and not params.get("dynamicTools")
                and no_mcp and all(config.get("features." + name) is False for name in (
                    "shell_tool", "unified_exec", "multi_agent", "apps", "plugins")))

    def _frontend_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        identifier = message.get("id")
        with self._lock:
            if method in ("account/login/start", "account/logout"):
                self._send_front({"id": identifier, "error": {"code": -32000,
                    "message": tr("Use the Launcher Accounts page to sign in or sign out")}})
                return
            backend_unavailable = self._process is None or self._process.poll() is not None
            if self._switching or (self._state == "needs_attention" and backend_unavailable):
                self._send_front({"id": identifier, "error": {"code": -32000,
                    "message": tr("Handoff requires verification; this request was not executed")}})
                return
            if method and identifier is not None:
                if identifier in self._pending or str(identifier).startswith("vibe-control-"):
                    raise CodexControlError("Duplicate or reserved native request ID")
                if len(self._pending) >= 256:
                    raise CodexControlError("Too many outstanding native requests")
                params = message.get("params") or {}
                self._pending[identifier] = (method, params, time.monotonic())
                if method == "initialize":
                    self._init = copy.deepcopy(params)
                if method == "thread/start" and self._tool_free_side_thread(params):
                    self._side_requests.add(identifier)
                if method in ("turn/start", "turn/steer") and params.get("threadId") not in self._side_threads:
                    if method == "turn/start":
                        self._turn_id = None
                    self._continuation_ready = False
                    self._state = "running"
            elif method is None:
                self._approvals.discard(identifier)
        self._write(message)

    def _send_front(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
        if len(payload) > _BRIDGE_LIMIT:
            raise CodexControlError("Native message exceeds size limit")
        with self._front_write:
            frontend = self._frontend
            if frontend:
                chunks = [payload[start:start + _BRIDGE_FRAME] for start in range(0, len(payload), _BRIDGE_FRAME)] or [b""]
                for index, chunk in enumerate(chunks):
                    self._frame(frontend, 0 if index else 1, chunk, final=index == len(chunks) - 1)

    @staticmethod
    def _frame(conn: socket.socket, opcode: int, payload: bytes, final: bool = True) -> None:
        size = len(payload)
        first = (0x80 if final else 0) | opcode
        prefix = bytes((first, size)) if size < 126 else (
            bytes((first, 126)) + struct.pack("!H", size) if size < 65536 else
            bytes((first, 127)) + struct.pack("!Q", size))
        conn.sendall(prefix + payload)

    @staticmethod
    def _exact(conn: socket.socket, size: int, deadline: float | None = None) -> bytes:
        data = bytearray()
        while len(data) < size:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("WebSocket receive deadline exceeded")
                conn.settimeout(remaining)
            chunk = conn.recv(size - len(data))
            if not chunk:
                raise EOFError
            data.extend(chunk)
        return bytes(data)

    def _accept(self) -> None:
        listener = self._listener
        while not self._closed and listener is not None:
            try:
                conn, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._client_slots.acquire(blocking=False):
                conn.close()
                continue
            try:
                threading.Thread(target=self._serve, args=(conn,), daemon=True).start()
            except Exception:
                conn.close()
                self._client_slots.release()
                raise

    def _serve(self, conn: socket.socket) -> None:
        owned = False
        try:
            conn.settimeout(self._deadline)
            handshake_deadline = time.monotonic() + self._deadline
            header = bytearray()
            while not header.endswith(b"\r\n\r\n"):
                header.extend(self._exact(conn, 1, handshake_deadline))
                if len(header) > 16384:
                    raise CodexControlError("WebSocket handshake exceeds limit")
            lines = header.decode("ascii").split("\r\n")
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    key = key.lower().strip()
                    if key in headers:
                        raise CodexControlError("Duplicate handshake header")
                    headers[key] = value.strip()
            key = headers.get("sec-websocket-key", "")
            if (lines[0] != "GET / HTTP/1.1" or headers.get("upgrade", "").lower() != "websocket"
                    or "upgrade" not in [part.strip() for part in headers.get("connection", "").lower().split(",")]
                    or headers.get("sec-websocket-version") != "13"
                    or len(base64.b64decode(key, validate=True)) != 16
                    or "origin" in headers
                    or not hmac.compare_digest(headers.get("authorization", ""), "Bearer " + self._capability)):
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            with self._lock:
                if self._frontend is not None:
                    conn.sendall(b"HTTP/1.1 409 Conflict\r\nContent-Length: 0\r\n\r\n")
                    return
                self._frontend = conn
                owned = True
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            conn.settimeout(self._deadline)
            fragments = bytearray()
            fragmented = False
            fragment_deadline = None
            while not self._closed:
                wait = 0.5
                if fragment_deadline is not None:
                    wait = min(wait, fragment_deadline - time.monotonic())
                    if wait <= 0:
                        raise TimeoutError("WebSocket fragmented message deadline exceeded")
                if not select.select([conn], [], [], wait)[0]:
                    continue
                frame_deadline = time.monotonic() + self._deadline
                if fragment_deadline is not None:
                    frame_deadline = min(frame_deadline, fragment_deadline)
                first, second = self._exact(conn, 2, frame_deadline)
                final, opcode = bool(first & 0x80), first & 0x0F
                size = second & 0x7F
                if first & 0x70 or not second & 0x80:
                    raise CodexControlError("Invalid client WebSocket frame")
                if size == 126:
                    size = struct.unpack("!H", self._exact(conn, 2, frame_deadline))[0]
                elif size == 127:
                    size = struct.unpack("!Q", self._exact(conn, 8, frame_deadline))[0]
                if size > _BRIDGE_LIMIT or len(fragments) + size > _BRIDGE_LIMIT:
                    raise CodexControlError("WebSocket message exceeds limit")
                if opcode >= 8 and (not final or size > 125):
                    raise CodexControlError("Invalid WebSocket control frame")
                mask = self._exact(conn, 4, frame_deadline)
                raw = self._exact(conn, size, frame_deadline)
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(raw))
                if opcode == 8:
                    with self._front_write:
                        self._frame(conn, 8, b"")
                    break
                if opcode == 9:
                    with self._front_write:
                        self._frame(conn, 10, payload)
                    continue
                if opcode == 10:
                    continue
                if opcode not in (0, 1) or (opcode == 0) != fragmented:
                    raise CodexControlError("Invalid WebSocket continuation")
                fragments.extend(payload)
                fragmented = not final
                if fragmented and fragment_deadline is None:
                    fragment_deadline = frame_deadline
                if final:
                    fragment_deadline = None
                    message = json.loads(fragments.decode("utf-8"))
                    fragments.clear()
                    if not isinstance(message, dict):
                        raise CodexControlError("Invalid native client message")
                    self._frontend_message(message)
        except Exception:
            if owned:
                with self._lock:
                    self._attention(tr("Native frontend disconnected; inspect execution before switching"))
        finally:
            with self._lock:
                if owned and self._frontend is conn:
                    self._frontend = None
            conn.close()
            self._client_slots.release()

    def _stop_backend(self) -> None:
        with self._lock:
            process = self._process
            self._generation += 1
        if process is None:
            return
        forced = False
        try:
            process.stdin.close()
            process.wait(timeout=self._deadline)
        except subprocess.TimeoutExpired:
            forced = True
            process.kill()
            process.wait(timeout=self._deadline)
        finally:
            if process.poll() is not None:
                process.stdout.close()
                with self._lock:
                    if self._process is process:
                        self._process = None
        if process.poll() is None:
            raise CodexControlError(tr("Previous execution owner did not exit"))
        if forced:
            raise CodexControlError(tr("Previous owner required forced termination; outcome unknown"))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._state = "closed"
            if self._listener:
                self._listener.close()
            if self._frontend:
                self._frontend.close()
        try:
            self._stop_backend()
        except CodexControlError:
            if self._process is not None:
                raise



# --- managed Claude Code: the CLI runs in this terminal, hooks report its events, a switch restarts it on another
# account with --resume of the same session. Design and real-account evidence: docs/claude-switch-design.md ----------
CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart",
                 "SubagentStop", "Stop", "StopFailure", "SessionEnd", "PostModelSwitch", "PermissionRequest")
CLAUDE_EVENT_FIELDS = ("hook_event_name", "session_id", "tool_use_id", "agent_id", "error", "source", "reason", "model",
                       "effort", "permission_mode")
CLAUDE_PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk")
CLAUDE_CONTINUE = "Continue the unfinished task from its existing checkpoint. Review completed tool actions; do not repeat them."
# Events that start work run as synchronous hooks: the CLI waits until the event is on disk, and a hook that cannot run,
# fails or times out blocks the action (``onFailure``, Claude Code 2.1.295+). The others only end work and stay
# asynchronous; a late one keeps the session looking busy, never idle.
CLAUDE_GATED = ("UserPromptSubmit", "PreToolUse")
CLAUDE_SYNC = CLAUDE_GATED + ("SessionStart", "SubagentStart", "PermissionRequest")
CLAUDE_MINIMUM = (2, 1, 295)
CLAUDE_HOOK_TIMEOUT = 10
CLAUDE_EVENT_BATCH = 256            # events observed per read; a longer backlog is never taken as a safe point


def claude_handoff_lock(events: Path) -> Path:
    return events.parent / "handoff.lock"


def claude_handoff_locked(events: Path) -> bool:
    """Is a handoff in progress? Held by a supervisor that still runs; a lock of one that died is ignored."""
    lock = claude_handoff_lock(events)
    try:
        owner = json.loads(read_private(lock, limit=4096)).get("pid")
    except FileNotFoundError:
        return False
    except (OSError, ValueError, AttributeError):
        return True                     # being written, or unreadable: treat it as held
    return process_alive(owner) is not False


def write_claude_event(events: Path, event: dict) -> None:
    """Publish events in one host-local order, independent of clock precision or process IDs.

    The OS releases the lock if a hook dies. Reserve the sequence before publishing: a failed write leaves a gap,
    never an overwritten event. The consumer sees only the complete, atomically published JSON files.
    """
    import errno
    private_dir(events)
    lock = events.parent / "event-sequence.lock"
    counter = events.parent / "event-sequence.json"
    assert_private_paths((lock, counter))
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Invalid event sequence lock")
        protect_path(lock)
        if WINDOWS:
            import msvcrt
        else:
            import fcntl
        deadline = time.monotonic() + 5
        while True:
            try:
                if WINDOWS:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or time.monotonic() >= deadline:
                    raise RuntimeError("Cannot reserve a Claude event sequence") from None
                time.sleep(0.01)
        try:
            previous = json.loads(read_private(counter, limit=4096)).get("sequence")
        except FileNotFoundError:
            previous = 0
        if type(previous) is not int or not 0 <= previous < 10 ** 20 - 1:
            raise ValueError("Invalid Claude event sequence")
        sequence = previous + 1
        path = events / ("%020d.json" % sequence)
        assert_private_path(path)
        if path.exists():
            raise ValueError("Claude event sequence would overwrite an event")
        write_json(counter, {"sequence": sequence})
        write_json(path, event)


def claude_hook(args: list) -> int:
    """Hook and status line command of a managed Claude session. A work-starting event is recorded before the CLI may
    go on (exit 0); during a handoff, or when it cannot be recorded, it is blocked (exit 2). Other errors are swallowed."""
    kind = args[0] if args else ""
    configured = args[1] if len(args) > 1 else ""      # the event this hook was registered for (claude_settings)
    name = ""
    try:
        raw = sys.stdin.buffer.read(1048577)
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("Unexpected hook input")
        name = data.get("hook_event_name") if isinstance(data.get("hook_event_name"), str) else ""
        if kind == "event" and os.environ.get("VIBE_CLAUDE_CONTROL"):
            event = {key: data[key] for key in CLAUDE_EVENT_FIELDS
                     if isinstance(data.get(key), (str, int, float, bool)) and len(str(data[key])) <= 256}
            tasks = data.get("background_tasks")
            event["background_tasks"] = len(tasks) if isinstance(tasks, (list, dict)) else (tasks if type(tasks) is int else 0)
            event["generation"] = os.environ.get("VIBE_CLAUDE_GENERATION", "")
            folder = Path(os.environ["VIBE_CLAUDE_CONTROL"])
            # Record first, then look at the lock; the supervisor takes the lock, then reads the events. Whichever
            # comes second sees the other: the work is either known to the handoff or refused.
            write_claude_event(folder, event)
            if (name or configured) in CLAUDE_GATED and claude_handoff_locked(folder):
                write_claude_event(folder,
                           {"hook_event_name": "VibeRefused", "refused": name, "tool_use_id": event.get("tool_use_id"),
                            "generation": event["generation"]})
                sys.stderr.write(tr("An account handoff is in progress; send this again in a moment.") + "\n")
                return 2
        elif kind == "status":
            if os.environ.get("VIBE_CLAUDE_CONTROL"):
                record_claude_status(data, Path(os.environ["VIBE_CLAUDE_CONTROL"]))
            if os.environ.get("VIBE_CLAUDE_QUOTA"):
                record_claude_quota(data.get("rate_limits"), Path(os.environ["VIBE_CLAUDE_QUOTA"]))
            if os.environ.get("VIBE_CLAUDE_STATUSLINE"):
                sys.stdout.buffer.write(run_user_statusline(os.environ["VIBE_CLAUDE_STATUSLINE"], raw))
                sys.stdout.flush()
    except Exception:
        # Work the supervisor could not be told about must not start; anything else never stops the CLI.
        if kind == "event" and os.environ.get("VIBE_CLAUDE_CONTROL") and (name or configured) in CLAUDE_GATED:
            return 2
    return 0


def record_claude_status(data: dict, events: Path) -> None:
    """The status line runs when a session starts or resumes and carries the model (SessionStart does not): record the
    session and model as an event, once per change, so a handoff can verify what the target really opened."""
    model = data.get("model")
    model = model.get("id") if isinstance(model, dict) else None
    session = data.get("session_id")
    if not isinstance(model, str) or not isinstance(session, str) or not 0 < len(model) <= 256 or len(session) > 256:
        return
    generation = os.environ.get("VIBE_CLAUDE_GENERATION", "")
    marker = events.parent / ("status-" + safe_id(generation or "0") + ".json")
    seen = {"session_id": session, "model": model}
    try:
        if json.loads(read_private(marker, limit=4096)) == seen:
            return
    except (OSError, ValueError):
        pass
    write_claude_event(events,
               dict(seen, hook_event_name="VibeStatus", generation=generation))
    write_json(marker, seen)


def _git_bash() -> str | None:
    """The Git Bash Claude Code uses on Windows (never WSL's bash.exe from System32)."""
    configured = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if configured and Path(configured).is_file():
        return configured
    git = shutil.which("git")
    if not git:
        return None
    base = Path(git).resolve().parent.parent
    for candidate in (base / "bin" / "bash.exe", base / "usr" / "bin" / "bash.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def run_user_statusline(command: str, data: bytes) -> bytes:
    """The user's own status line, run the way Claude Code runs it (Windows: Git Bash, else PowerShell; sh elsewhere)."""
    if WINDOWS:
        bash = _git_bash()
        argv = [bash, "-c", command] if bash else ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        argv = ["/bin/sh", "-c", command]
    try:
        result = subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
                                creationflags=0x08000000 if WINDOWS else 0)
    except (OSError, subprocess.SubprocessError):
        return b""
    return result.stdout[:65536]


def record_claude_quota(limits: object, path: Path) -> None:
    """The status line's 5-hour / 7-day usage, in the shared quota cache format; rewritten only when it changed or aged."""
    if not isinstance(limits, dict):
        return
    windows = []
    for slot, minutes in (("five_hour", 300), ("seven_day", 10080)):
        item = limits.get(slot)
        used, resets = (item.get("used_percentage"), item.get("resets_at")) if isinstance(item, dict) else (None, None)
        if type(used) in (int, float) and math.isfinite(used) and type(resets) is int and resets > 0:
            windows.append({"slot": slot, "used_percent": max(0, min(100, used)), "window_minutes": minutes, "resets_at": resets})
    if not windows:
        return
    try:
        old = json.loads(read_private(path, limit=65536))
        if old.get("windows") == windows and time.time() - _quota_timestamp(old.get("checked_at")) < 60:
            return
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    write_json(path, {"tool": "claude", "account_id": os.environ.get("VIBE_CLAUDE_ACCOUNT", ""), "plan": None,
                      "environment_id": os.environ.get("VIBE_CLAUDE_ENVIRONMENT", "local"), "windows": windows,
                      "reached": None, "credits": None, "reset_credits": 0, "checked_at": now()})


def claude_settings(prefix: str, user_statusline: dict | None = None) -> dict:
    """Settings for this launch only (``--settings``): hook lists merge with the user's own; nothing on disk changes.
    The status line is a single value, so the launcher's records the quota and then runs the user's own (see claude_hook)."""
    def hook(name: str) -> list:
        entry = {"type": "command", "command": prefix + " event " + name}
        if name in CLAUDE_SYNC:
            entry.update(timeout=CLAUDE_HOOK_TIMEOUT, onFailure="block")
        else:
            entry.update({"async": True, "timeout": 30})
        return [{"hooks": [entry]}]

    settings = {"hooks": {name: hook(name) for name in CLAUDE_EVENTS},
                "statusLine": {"type": "command", "command": prefix + " status"}}
    padding = (user_statusline or {}).get("padding")
    if type(padding) is int and 0 <= padding <= 20:
        settings["statusLine"]["padding"] = padding
    return settings


def claude_user_statusline(profile: Path, project: Path) -> dict | None:
    """The status line the user would see: project local, then project, then user settings (Claude's precedence)."""
    for path in (project / ".claude" / "settings.local.json", project / ".claude" / "settings.json", profile / "settings.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        line = data.get("statusLine") if isinstance(data, dict) else None
        if isinstance(line, dict) and line.get("type") == "command" and isinstance(line.get("command"), str):
            return {"command": line["command"][:4096], "padding": line.get("padding")}
    return None


def managed_claude_supported(executable: str, env: dict, root: Path) -> bool:
    try:
        probe = subprocess.run([executable, "--help"], env=env, cwd=root, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    if probe.returncode or not all(flag in probe.stdout for flag in (b"--session-id", b"--settings", b"--resume")):
        return False
    # Blocking hooks that fail (onFailure) are what keeps work from starting unseen during a handoff.
    try:
        probe = subprocess.run([executable, "--version"], env=env, cwd=root, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    found = re.search(rb"(\d+)\.(\d+)\.(\d+)", probe.stdout or b"")
    return not probe.returncode and found is not None and tuple(map(int, found.groups())) >= CLAUDE_MINIMUM


def latest_claude_session(profile: Path, project: Path) -> str | None:
    """The newest conversation of this project in the profile: an exact id to resume, never a guess at run time."""
    folder = profile / "projects" / claude_project_folder(str(project))
    try:
        newest = max((path for path in folder.glob("*.jsonl") if _plain(path)), key=lambda path: path.stat().st_mtime)
        return safe_id(newest.stem)
    except (OSError, ValueError):
        return None


def _copy_private_tree(source: Path, target: Path, limit: int = 4000) -> None:
    files = [path for path in source.rglob("*") if path.is_file() and _plain(path)][:limit] if source.is_dir() else []
    for path in files:
        _write_private(target / path.relative_to(source), path.read_bytes())


def copy_claude_session(source: Path, target: Path, session_id: str) -> None:
    """Hand one conversation to another profile at the same place: its transcript, subagent and tool-result files,
    file-history checkpoints and todos. The target's older copy, if any, is replaced by the newer one."""
    found = [path for path in (source / "projects").glob("*/" + safe_id(session_id) + ".jsonl") if _plain(path)]
    if len(found) != 1:
        raise CodexControlError(tr("The conversation transcript was not found"))
    folder = found[0].parent.name
    _write_private(target / "projects" / folder / found[0].name, found[0].read_bytes())
    _copy_private_tree(source / "projects" / folder / session_id, target / "projects" / folder / session_id)
    _copy_private_tree(source / "file-history" / session_id, target / "file-history" / session_id)
    for path in list((source / "todos").glob(session_id + "*.json"))[:64] if (source / "todos").is_dir() else []:
        if _plain(path):
            _write_private(target / "todos" / path.name, path.read_bytes())


def claude_profile_fingerprint(account: dict, root: Path, explicit: tuple = ()) -> dict:
    """Environment-level Claude state: shared folders, notes and the shared part of settings.json, without the
    ``explicit`` keys the launch sets with flags (the conversation's own model and effort)."""
    profile = profile_dir(root, account)
    result = {}
    for relative in _shareable_files(profile, "claude"):
        data = read_private_bytes(profile / relative)
        if len(data) > SHARE_MAX_FILE:
            raise ValueError("An extension exceeds the safe comparison limit")
        result[str(relative)] = hashlib.sha256(data).hexdigest()
    try:
        settings = json.loads(read_private(profile / "settings.json"))
        shared, env = shared_settings(settings if isinstance(settings, dict) else {})
        shared = {key: value for key, value in shared.items() if key not in explicit}
        result["settings.json"] = hashlib.sha256(json.dumps([shared, env], sort_keys=True).encode()).hexdigest()
    except FileNotFoundError:
        pass
    return result


def claude_capabilities(profile: Path, project: Path) -> tuple[set, dict]:
    """Account-level capabilities: enabled plugins and MCP servers (names and transport only; nothing else is used)."""
    plugins, servers = set(), {}
    try:
        enabled = json.loads(read_private(profile / "settings.json")).get("enabledPlugins")
        plugins = {name for name, value in enabled.items() if value} if isinstance(enabled, dict) else set()
    except (OSError, ValueError, AttributeError):
        pass
    try:
        data = json.loads(read_private(profile / ".claude.json", limit=16 * 1048576))
    except (OSError, ValueError):
        data = {}
    key = str(project).replace("\\", "/").casefold()
    scopes = [data.get("mcpServers")] + [value.get("mcpServers") for name, value in (data.get("projects") or {}).items()
                                         if isinstance(value, dict) and name.replace("\\", "/").casefold() == key]
    for scope in scopes:
        for name, server in (scope or {}).items() if isinstance(scope, dict) else []:
            if isinstance(name, str) and isinstance(server, dict):
                servers[name[:64]] = server.get("type") if server.get("type") in ("http", "sse") else "stdio"
    return plugins, servers


class ClaudeSupervisor:
    """The bridge interface (see ManagedCodexBridge) for Claude Code. Events come from hooks the CLI runs; a switch takes
    the handoff lock at a turn boundary (no new prompt or tool can start), ends the CLI and starts it again on the target
    account with ``--resume`` of the session. The target becomes the active account only once it reported the session;
    otherwise the source account is started again on the unchanged conversation."""
    VERIFY_SECONDS = 60
    def __init__(self, request: dict, root: Path, accounts: dict, command_for: Callable):
        self._request, self._root, self._accounts, self._command_for = request, root, accounts, command_for
        self._lock = threading.RLock()
        self._account = request["account"]["id"]
        self._session = request.get("resume_thread_id") or str(uuid.uuid4())
        self._resume = bool(request.get("resume_thread_id"))
        self._events = codex_control_path(root, request["session_id"]) / "events"
        self._process = None
        self._generation = 0
        self._ours = False
        self._state, self._reason = "starting", ""
        self._pending_account = None
        self._blocked = None
        self._switching = False
        self._last_turn_status = self._last_error_kind = self._last_error_at = None
        self._continuation_ready, self._continuation_until = False, 0.0
        self._live: dict = {}
        self._validate_target = None
        self._verifying: dict | None = None     # a started target that has not yet reported the session
        # The full model id the running CLI itself reported for this session (status line, or SessionStart when it has
        # one). A handoff needs it: the target starts on exactly this id and must report exactly this id back.
        self._model_evidence: str | None = None
        self._reset()

    def _reset(self) -> None:
        self._started, self._prompts, self._stops, self._background = False, 0, 0, 0
        self._tools, self._agents = set(), set()

    def start(self) -> None:
        private_dir(self._events)
        for path in self._events.glob("*.json"):
            path.unlink(missing_ok=True)
        claude_handoff_lock(self._events).unlink(missing_ok=True)      # this terminal's own, left by a worker that died
        self._spawn(self._account, None)

    def _quiesce(self) -> bool:
        """Take the handoff lock, then read every recorded event: True (lock held) when nothing runs; otherwise the lock
        is released at once and work goes on. A prompt or tool after this point is refused by its hook."""
        lock = claude_handoff_lock(self._events)
        assert_private_path(lock)
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            protect_path(lock)
            json.dump({"pid": os.getpid()}, stream)
        # Every event recorded before the lock must be observed; a backlog beyond one batch postpones the handoff.
        complete = self._read_events()
        with self._lock:
            if (complete and self._started and self._prompts <= self._stops and not self._tools and not self._agents
                    and not self._background and self._process is not None and self._process.poll() is None):
                return True
        self._release()
        return False

    def _release(self) -> None:
        claude_handoff_lock(self._events).unlink(missing_ok=True)

    def _settings_snapshot(self) -> dict:
        """The conversation's settings a restart carries: model (only the proven id), effort and permission mode."""
        return dict(self._live, model=self._model_evidence)

    def _spawn(self, account_id: str, prompt: str | None, live: dict | None = None) -> None:
        with self._lock:
            self._generation += 1
            self._reset()
            self._model_evidence = None         # a new process proves its model again
            self._state, self._ours = "starting", False
            args = ["--resume", self._session] if self._resume else ["--session-id", self._session]
            self._resume = True             # every later start continues this conversation
            environment = self._request.get("environment_id") or "local"
            account = self._accounts[account_id]
            user_line = claude_user_statusline(profile_dir(self._root, account), Path(self._request["path"]))
            env = dict(prepare(account, self._root), VIBE_CLAUDE_CONTROL=str(self._events),
                       VIBE_CLAUDE_STATUSLINE=(user_line or {}).get("command", ""),
                       VIBE_CLAUDE_GENERATION=str(self._generation), VIBE_CLAUDE_ACCOUNT=account_id,
                       VIBE_CLAUDE_ENVIRONMENT=environment, VIBE_CLAUDE_QUOTA=str(quota_file(self._root, account_id, environment)))
            live = dict(self._live if live is None else live)
            self._process = subprocess.Popen(self._command_for(account_id, args, prompt, live),
                                             cwd=self._request["path"], env=env)

    def finished(self) -> int | None:
        """The CLI's exit code when the user ended it (not when the supervisor ended it to switch)."""
        with self._lock:
            process = self._process
            if process is None or self._switching or self._ours:
                return None
            return process.poll()

    @property
    def pid(self) -> int | None:
        return getattr(self._process, "pid", None)

    def snapshot(self) -> dict:
        with self._lock:
            return {"state": "switching" if self._verifying else self._state, "account": self._account,
                    "thread_id": self._session,
                    "pending_account": self._pending_account, "reason": self._reason,
                    "last_turn_status": self._last_turn_status, "last_error_kind": self._last_error_kind,
                    "last_error_at": self._last_error_at, "rate_limits": None, "rate_limits_checked_at": None,
                    "rate_limit_times": {}, "backend_pid": self.pid, "blocked": copy.deepcopy(self._blocked),
                    "continuation_ready": self._continuation_ready and time.monotonic() < self._continuation_until,
                    "active_tools": len(self._tools), "live": dict(self._live), "can_switch": self._safe()}

    def request_switch(self, target_account: str) -> dict:
        with self._lock:
            if not target_account or self._state == "closed":
                raise CodexControlError(tr("Invalid switch request"))
            if target_account == self._account:
                return self.snapshot()
            if self._pending_account and self._pending_account != target_account:
                raise CodexControlError(tr("Another account switch is pending"))
            self._pending_account, self._blocked = target_account, None
        return self.snapshot()

    def cancel_switch(self) -> dict:
        with self._lock:
            if self._switching:
                raise CodexControlError(tr("Handoff is already in progress"))
            self._pending_account = None
            if self._state == "needs_attention" and self._process is not None and self._process.poll() is None:
                self._state, self._reason = "unknown", tr("Switch cancelled; original account remains active")
        return self.snapshot()

    def _safe(self) -> bool:
        return bool(self._started and self._state == "idle" and self._prompts <= self._stops and not self._tools
                    and not self._agents and not self._background and not self._switching and not self._verifying
                    and self._process is not None and self._process.poll() is None)

    def _read_events(self) -> bool:
        """Observe recorded events in order, at most CLAUDE_EVENT_BATCH per call. True only when the queue was emptied:
        anything still waiting may be work that already started."""
        paths = sorted(self._events.glob("*.json"))
        for path in paths[:CLAUDE_EVENT_BATCH]:
            try:
                event = json.loads(read_private(path, limit=65536))
            except (OSError, ValueError):
                event = None
            path.unlink(missing_ok=True)
            if isinstance(event, dict) and event.get("generation") == str(self._generation):
                with self._lock:
                    self._observe(event)
        return len(paths) <= CLAUDE_EVENT_BATCH and not any(self._events.glob("*.json"))

    def _observe(self, event: dict) -> None:
        name = event.get("hook_event_name")
        if name == "VibeRefused":
            # The hook refused this prompt or tool during a handoff: it never started.
            if event.get("refused") == "UserPromptSubmit":
                self._prompts = max(self._stops, self._prompts - 1)
                if self._started and self._prompts <= self._stops:
                    self._state = "idle"
            else:
                self._tools.discard(str(event.get("tool_use_id")))
            return
        if name == "VibeStatus":
            # The status line's report: the model a session runs (SessionStart does not carry it).
            if self._verifying is not None:
                self._verifying["status_session"], self._verifying["model"] = event.get("session_id"), event.get("model")
            elif isinstance(event.get("model"), str) and event.get("session_id") == self._session:
                self._live["model"] = self._model_evidence = event["model"]
            return
        if name == "SessionStart" and self._verifying is not None:
            # What the target says it opened; checked against the expected conversation and model before commit.
            self._verifying["reported_session"] = event.get("session_id")
            self._started = True
            return
        for key in ("model", "effort", "permission_mode"):
            if isinstance(event.get(key), str) and event[key]:
                self._live[key] = event[key]
        if name == "PostModelSwitch":
            self._model_evidence = None         # what it switched to (perhaps an alias) is proven by the next status line
        elif name == "SessionStart" and isinstance(event.get("model"), str) and event["model"]:
            self._model_evidence = event["model"]
        if name == "SessionStart":
            if isinstance(event.get("session_id"), str):
                self._session = safe_id(event["session_id"])     # /clear starts another conversation: follow it
            self._started = True
            self._state = "idle" if self._prompts <= self._stops else "running"
        elif name == "UserPromptSubmit":
            self._prompts += 1
            self._continuation_ready = False
            self._state = "running"
        elif name == "PreToolUse":
            self._tools.add(str(event.get("tool_use_id")))
        elif name in ("PostToolUse", "PostToolUseFailure"):
            self._tools.discard(str(event.get("tool_use_id")))
        elif name == "SubagentStart":
            self._agents.add(str(event.get("agent_id")))
        elif name == "SubagentStop":
            self._agents.discard(str(event.get("agent_id")))
        elif name in ("Stop", "StopFailure"):
            self._stops += 1
            self._background = event.get("background_tasks") if type(event.get("background_tasks")) is int else 0
            failed = name == "StopFailure"
            self._last_turn_status = "failed" if failed else "completed"
            self._last_error_kind = "usageLimitExceeded" if failed and event.get("error") == "rate_limit" else None
            self._last_error_at = time.time() if self._last_error_kind else None
            if self._prompts <= self._stops:
                self._state = "idle"
        elif name == "SessionEnd":
            self._started = False

    def tick(self) -> dict:
        self._read_events()
        if self._verifying is not None:
            return self._verify_target()
        with self._lock:
            if not self._pending_account or not self._safe():
                return self.snapshot()
            target, source = self._pending_account, self._account
            if self._live.get("permission_mode") == "bypassPermissions":
                self._attention(tr("Sessions that bypass permissions are switched by hand"))
                return self.snapshot()
            if not self._model_evidence:
                # Never fall back to an account's default model: the conversation would silently change model.
                self._reason = tr("Waiting for Claude to report the current model; the account switch starts after that.")
                return self.snapshot()
            self._switching, self._state = True, "switching"
            live = self._settings_snapshot()     # checked against the target now, confirmed again under the lock
        stopped = locked = said = False
        try:
            if self._validate_target:
                self._validate_target(target, dict(live))
            locked = self._quiesce()
            if locked:
                with self._lock:
                    changed = self._settings_snapshot() != live
                if changed:
                    # The user changed model, effort or mode while the target was checked: nothing is stopped; the
                    # next safe point checks the target again against the new settings.
                    self._release()
                    locked = False
                    with self._lock:
                        self._reason = tr("Settings changed while the switch was prepared; checking the target again.")
            if not locked:
                with self._lock:            # work started meanwhile: the switch waits for the next safe point
                    self._state = "running" if self._prompts > self._stops else "idle"
                return self.snapshot()
            source_profile = profile_dir(self._root, self._accounts[source])
            # Nothing said yet: there is no transcript to move, and --resume would find no conversation.
            said = any(_plain(found) for found in (source_profile / "projects").glob("*/" + safe_id(self._session) + ".jsonl"))
            self._end_cli()
            stopped = True
            if said:
                copy_claude_session(source_profile, profile_dir(self._root, self._accounts[target]), self._session)
            else:
                with self._lock:
                    self._resume = False      # the target starts this (still empty) conversation under the same id
            # a target never opened here would stop on the first-run wizard and swallow the continuation
            carry_claude_onboarding(self._accounts[source], self._accounts[target], self._root, self._request["path"])
            with self._lock:
                self._verifying = {"source": source, "target": target, "said": said, "live": live,
                                   "expected_session": self._session,
                                   "expected_model": live["model"],
                                   "until": time.monotonic() + self.VERIFY_SECONDS}
            self._release()                 # the old CLI has ended; the target's own prompts must pass
            locked = False
            self._spawn(target, None, live)
        except CodexHandoffBlocked as error:
            with self._lock:
                self._pending_account = None
                self._blocked = {"target": target, "gaps": copy.deepcopy(error.gaps), "at": time.time()}
                self._reason, self._state = str(error), "idle"
        except Exception as error:
            if not stopped:
                with self._lock:
                    self._attention(str(error) if isinstance(error, CodexControlError)
                                    else tr("Account handoff could not be verified; reopen this exact thread manually"))
            else:
                self._rollback({"source": source, "said": said, "live": live},
                               tr("The switch to the target account failed; the original account was started again."))
        finally:
            if locked:
                self._release()
            with self._lock:
                if self._verifying is None:
                    self._switching = False
        return self.snapshot()

    def _verify_target(self) -> dict:
        """Commit the target account only when its CLI runs and reported this session with the model it was given."""
        with self._lock:
            verifying = self._verifying
            alive = self._process is not None and self._process.poll() is None
            expected, seen = verifying.get("expected_model"), verifying.get("model")
            # The exact conversation and exactly the model id the source reported. No alias is resolved: the target was
            # started with that full id, so it must report the same id; anything missing or different is not verified.
            verified = (verifying.get("reported_session") == verifying["expected_session"]
                        and verifying.get("status_session") == verifying["expected_session"]
                        and isinstance(expected, str) and bool(expected) and seen == expected)
            if alive and self._started and verified:
                self._state = "idle" if self._prompts <= self._stops else "running"
                self._account, self._pending_account, self._blocked, self._reason = verifying["target"], None, None, ""
                self._continuation_ready = self._last_error_kind in QUOTA_ERRORS
                self._continuation_until = time.monotonic() + 120
                self._verifying, self._switching = None, False
                self._model_evidence = seen
                return self.snapshot()
            # Still starting, or the right session reported and its status line not yet run: wait (within the limit).
            pending = not self._started or (verifying.get("reported_session") == verifying["expected_session"]
                                            and not verifying.get("status_session"))
            if alive and pending and time.monotonic() < verifying["until"]:
                return self.snapshot()
        self._rollback(verifying, tr("The target account could not take over the conversation; "
                                     "the original account was started again."))
        return self.snapshot()

    def _rollback(self, verifying: dict, message: str) -> None:
        """Bring the unchanged conversation back on the source account (the reported account never changed). If even that
        fails nothing runs, and the saved conversation is the checkpoint the user reopens."""
        self._end_cli()
        try:
            with self._lock:
                self._live = dict(verifying["live"])
                if not verifying["said"]:
                    self._resume = False
                self._verifying = None
            self._spawn(verifying["source"], None, verifying["live"])
            with self._lock:
                self._pending_account, self._switching = None, False
                self._reason = message
        except Exception:
            with self._lock:
                self._pending_account, self._verifying, self._switching = None, None, False
                self._attention(tr("Account handoff failed and the original account could not be started; "
                                   "reopen this exact conversation manually."))
                self._ours = False            # nothing runs any more: let the worker end

    def simulate_quota_failure(self) -> bool:
        """Test only (scripts/simulate_quota.py): mark the last completed turn as stopped by quota."""
        with self._lock:
            if not self._safe() or self._last_turn_status != "completed":
                return False
            self._last_turn_status, self._last_error_kind, self._last_error_at = "failed", "usageLimitExceeded", time.time()
            return True

    def resume_work(self, recovered: bool = False) -> dict:
        """Continue a task that stopped on quota: start the CLI again on this conversation with the continue prompt."""
        with self._lock:
            ready = ((self._continuation_ready and time.monotonic() < self._continuation_until)
                     or (recovered and self._last_error_kind in QUOTA_ERRORS))
            if not ready or not self._safe():
                raise CodexControlError(tr("No verified quota checkpoint is available for continuation"))
        if not self._quiesce():
            raise CodexControlError(tr("No verified quota checkpoint is available for continuation"))
        with self._lock:
            self._continuation_ready, self._switching = False, True
        try:
            self._end_cli()
            self._release()
            self._spawn(self._account, CLAUDE_CONTINUE)
        finally:
            self._release()
            with self._lock:
                self._switching = False
        return self.snapshot()

    def _attention(self, reason: str) -> None:
        self._state, self._reason = "needs_attention", reason

    def _end_cli(self) -> None:
        with self._lock:
            process, self._ours = self._process, True
        if process is None or process.poll() is not None:
            return
        stop_process(process)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)

    def close(self) -> None:
        with self._lock:
            self._state = "closed"
        self._end_cli()


class ManagedClaudeSession(ManagedCodexSession):
    """The Codex controller with Claude's checks; its quota is observed by the status line, never queried."""
    PASSIVE_QUOTA = True

    def validate_target(self, identifier: str, settings: dict) -> None:
        account = self.accounts.get(identifier)
        if not account or account.get("tool") != "claude" or not switch_supported(account) or not account_fits(account, self.environment):
            raise CodexControlError(tr("Target account is outside this environment"))
        env = prepare(account, self.root)
        if not check_login(account, self.root, env):
            raise CodexControlError(tr("Sign in to the target account before switching"))
        if sync_user_data(account, self.root, self.request["path"]).get("failed"):
            raise CodexControlError(tr("Target extensions could not be verified"))
        current = self.accounts[self.bridge.snapshot()["account"]]
        # The restart passes the conversation's model and effort as flags, so the profiles' defaults do not apply.
        explicit = (("model",) if settings.get("model") or current.get("model") else ()) + (("effortLevel",) if settings.get("effort") else ())
        source = claude_profile_fingerprint(current, self.root, explicit)
        target = claude_profile_fingerprint(account, self.root, explicit)
        differing = sorted({"/".join(Path(key).parts[:3]) for key in set(source) | set(target) if source.get(key) != target.get(key)})
        if differing:
            raise CodexControlError(tr("Account instructions or settings differ: {items}",
                                       items=", ".join(differing[:4]) + (" …" if len(differing) > 4 else "")))
        project = Path(self.request["path"])
        source_plugins, source_servers = claude_capabilities(profile_dir(self.root, current), project)
        target_plugins, target_servers = claude_capabilities(profile_dir(self.root, account), project)
        gaps = ([{"kind": "plugin", "name": name.split("@")[0][:64]} for name in sorted(source_plugins - target_plugins)]
                + [{"kind": "mcp", "name": name} for name in sorted(set(source_servers) - set(target_servers))]
                # OAuth sign-ins of remote servers stay in each profile's credentials, which are never read.
                + [{"kind": "mcp_unknown", "name": name} for name in sorted(source_servers)
                   if source_servers[name] != "stdio" and name in target_servers])
        self._accept_gaps(identifier, account, gaps[:24])
        self._target = account


def run_managed_claude(request: dict, root: Path, env: dict, stop_path: Path) -> int | None:
    """Run Claude Code under the supervisor. ``None``: this CLI cannot, run it unmanaged."""
    try:
        executable = trusted_executable("claude")
    except RuntimeError:
        return None
    folder = codex_control_path(root, request["session_id"])
    if not managed_claude_supported(executable, env, root):
        write_json(folder / "state.json", {"status": "unmanaged", "environment_id": request.get("environment_id") or "local",
                                           "account_id": request["account"]["id"], "message":
                                           tr("This Claude Code cannot run safe account management; update it to 2.1.295 or later to enable switching.")})
        print(tr("This Claude Code cannot hand the conversation to another account; it starts without account management."), flush=True)
        return None
    project = Path(request["path"])
    if not request.get("resume_thread_id") and request.get("resume") is True:
        latest = latest_claude_session(profile_dir(root, request["account"]), project)
        if latest:
            request = dict(request, resume_thread_id=latest)
    thread_id = request.get("resume_thread_id")
    if thread_id:
        deadline = time.monotonic() + 20
        while thread_owner_active(root, thread_id, request["session_id"]):
            if time.monotonic() > deadline:
                raise RuntimeError(tr("This conversation is still open in another terminal; close it there first."))
            time.sleep(0.5)
    prefix = request.get("claude_hook_command") or shlex.join([sys.executable, "-I", str(Path(__file__).resolve()), "--claude-hook"])
    accounts = {request["account"]["id"]: request["account"]}

    def command_for(account_id, mode_args, prompt, live):
        account = controller.accounts.get(account_id) or accounts[account_id]
        argv = build_cli(account, "launch", root, "device", project, False)
        argv[0] = executable
        if "--model" in argv:
            del argv[argv.index("--model"):argv.index("--model") + 2]
        model = live.get("model") or account.get("model")
        argv += ["--model", model] if model else []
        argv += ["--effort", live["effort"]] if live.get("effort") else []
        if live.get("permission_mode") in CLAUDE_PERMISSION_MODES:
            argv += ["--permission-mode", live["permission_mode"]]
        user_line = claude_user_statusline(profile_dir(root, account), project)
        argv += ["--settings", json.dumps(claude_settings(prefix, user_line))] + mode_args
        return argv + ([prompt] if prompt else [])

    handlers = {}
    if not WINDOWS and threading.current_thread() is threading.main_thread():
        for number, code in ((signal.SIGTERM, 143), (signal.SIGHUP, 129)):
            handlers[number] = signal.signal(number, lambda *_args, code=code: sys.exit(code))
    controller = None
    supervisor = ClaudeSupervisor(request, root, accounts, command_for)
    try:
        controller = ManagedClaudeSession(request, root, supervisor)
        supervisor._accounts = controller.accounts
        supervisor._validate_target = controller.validate_target
        supervisor.start()
        save_status(root, request, "running", pid=supervisor.pid)
        result = None
        while result is None:
            try:
                controller.tick()
            except Exception:
                controller.disable()
            if stop_path.exists():
                supervisor.close()
                result = 0
                break
            result = supervisor.finished()
            try:
                time.sleep(0.3)
            except KeyboardInterrupt:
                continue            # Ctrl+C belongs to the CLI
        save_status(root, request, "ended" if result == 0 or result in USER_QUIT_CODES else "failed")
        return result
    finally:
        if controller is not None:
            controller.close()
        else:
            supervisor.close()
        for number, handler in handlers.items():
            signal.signal(number, handler)
        stop_path.unlink(missing_ok=True)


def quota_file(root: Path, account_id: str, environment_id: str) -> Path:
    return root / "quota" / (safe_id(account_id) + "__" + safe_id(environment_id or "local") + ".json")


def switch_supported(account: dict) -> bool:
    """Subscription accounts whose conversations the launcher can hand between accounts (Codex and Claude Code)."""
    return account.get("tool") in ("codex", "claude") and account.get("auth") == "subscription"


def quota_supported(account: dict) -> bool:
    """Only a Codex subscription has a quota to ask about; an API key / endpoint is billed per use."""
    return account.get("tool") == "codex" and account.get("auth") == "subscription"


def rpc_rate_limits(command: list[str], env: dict, cwd: Path | None = None, timeout: float = 30.0) -> dict:
    """Ask `codex app-server` (the CLI's own JSON-RPC interface) for the account's rate limits.

    Codex itself talks to its backend with the profile's login; this launcher never reads or sends the token.
    """
    return rpc_request(command, env, "account/rateLimits/read", None, cwd, timeout)


def rpc_request(command: list[str], env: dict, method: str, params, cwd: Path | None = None, timeout: float = 30.0) -> dict:
    """One request to a short-lived `codex app-server` started with the profile's own environment."""
    return rpc_requests(command, env, [(method, params)], cwd, timeout)[0]


def rpc_requests(command: list[str], env: dict, calls: list, cwd: Path | None = None, timeout: float = 30.0) -> list:
    """Requests answered in order by one short-lived `codex app-server` with the profile's own environment."""
    flags = 0x08000000 if WINDOWS else 0          # CREATE_NO_WINDOW
    process = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, creationflags=flags)
    messages: queue.Queue = queue.Queue()

    def read() -> None:
        for raw in process.stdout:
            try:
                messages.put(json.loads(raw))
            except ValueError:
                pass
        messages.put(None)

    threading.Thread(target=read, daemon=True).start()

    def send(message: dict) -> None:
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        process.stdin.flush()

    def answer(request_id: int):
        deadline = time.monotonic() + timeout
        while True:
            try:
                message = messages.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                raise RuntimeError("Codex did not answer in time") from None
            if message is None:
                raise RuntimeError("Codex app-server stopped unexpectedly")
            if isinstance(message, dict) and "method" not in message and message.get("id") == request_id:
                if "error" in message:
                    detail = message["error"].get("message", "") if isinstance(message["error"], dict) else ""
                    raise RuntimeError("Codex reported: " + str(detail)[:200])
                return message.get("result") or {}
            if time.monotonic() > deadline:
                raise RuntimeError("Codex did not answer in time")

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "vibe-launcher", "title": "Vibe Launcher", "version": "1"}}})
        answer(1)
        send({"method": "initialized"})
        results = []
        for index, (method, params) in enumerate(calls, start=2):
            send({"id": index, "method": method, "params": params})
            results.append(answer(index))
        return results
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        process.kill()
        process.wait(5)


def quota_summary(result: dict) -> dict:
    """Reduce the app-server answer to what the UI shows: plan, each window's usage and reset time, credits."""
    snapshot = result.get("rateLimits") if isinstance(result, dict) else None
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    windows = []
    for slot in ("primary", "secondary"):
        item = snapshot.get(slot)
        if (isinstance(item, dict) and isinstance(item.get("usedPercent"), (int, float))
                and not isinstance(item.get("usedPercent"), bool) and math.isfinite(item["usedPercent"])):
            minutes, resets = item.get("windowDurationMins"), item.get("resetsAt")
            windows.append({"slot": slot, "used_percent": max(0, min(100, item["usedPercent"])),
                            "window_minutes": minutes if type(minutes) is int and minutes > 0 else None,
                            "resets_at": resets if type(resets) is int and resets > 0 else None})
    credits = snapshot.get("credits")
    credits = ({"unlimited": credits.get("unlimited") is True, "has_credits": credits.get("hasCredits") is True,
                "balance": credits.get("balance") if isinstance(credits.get("balance"), str) else None}
               if isinstance(credits, dict) else None)
    resets = result.get("rateLimitResetCredits") if isinstance(result, dict) else None
    count = resets.get("availableCount") if isinstance(resets, dict) else None
    plan = snapshot.get("planType")
    reached = snapshot.get("rateLimitReachedType")
    buckets = {}
    raw_buckets = result.get("rateLimitsByLimitId") if isinstance(result, dict) else None
    if isinstance(raw_buckets, dict):
        for limit_id, bucket in list(raw_buckets.items())[:128]:
            if isinstance(limit_id, str) and 0 < len(limit_id) <= 128 and isinstance(bucket, dict):
                parsed = quota_summary({"rateLimits": bucket})
                buckets[limit_id] = {key: parsed[key] for key in ("windows", "reached", "credits")}
                buckets[limit_id]["name"] = bucket.get("limitName") if isinstance(bucket.get("limitName"), str) else None
    elif snapshot:
        limit_id = snapshot.get("limitId")
        buckets[limit_id if isinstance(limit_id, str) and limit_id else "codex"] = {
            "windows": windows, "reached": reached if isinstance(reached, str) else None, "credits": credits,
            "name": None}
    return {"tool": "codex", "plan": plan if isinstance(plan, str) else None, "windows": windows, "credits": credits,
            "buckets": buckets,
            "reached": reached if isinstance(reached, str) else None,
            "reset_credits": count if isinstance(count, int) else 0, "checked_at": now()}


def fetch_quota(account: dict, root: Path, environment_id: str = "local") -> dict:
    """Query the quota on THIS machine, using the account's own profile, and remember the answer."""
    if not quota_supported(account):
        raise ValueError("Quota lookup is only available for Codex subscription accounts")
    env = prepare(account, root)
    summary = quota_summary(rpc_rate_limits([trusted_executable("codex"), "app-server"], env, profile_dir(root, account)))
    summary.update(account_id=account["id"], environment_id=environment_id or "local")
    return summary


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--claude-hook":
        return claude_hook(sys.argv[2:])        # a hook or status line command of a managed Claude session
    if len(sys.argv) == 3 and sys.argv[1] == "--worker-file":
        request = json.loads(read_private(Path(sys.argv[2])))
    else:
        request = json.loads(base64.urlsafe_b64decode(sys.argv[1]))
    root = Path.home() / ".local" / "share" / "vibe-launcher"
    global _worker_created
    _worker_created = False
    try:
        return perform(request, root)
    except KeyboardInterrupt:
        emit(request, {"status": "unknown"})
        return 130
    except Exception as error:
        # Never stringify subprocess exceptions: they can include credential env.
        print("Vibe Launcher: " + (str(error) if isinstance(error, (ValueError, RuntimeError))
                                  else type(error).__name__), file=sys.stderr)
        emit(request, {"status": "failed"})
        return EXIT_NOT_STARTED if request.get("action") == "launch" and not _worker_created else 1


def github_env(account: dict, root: Path, parent: dict | None = None) -> dict:
    env = dict(os.environ if parent is None else parent)
    for key in list(env):
        if key.upper().startswith(("GH_", "GITHUB_", "GIT_CONFIG_")) or key.upper() in {
                "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_ASKPASS", "SSH_ASKPASS"}:
            del env[key]
    profile = root / "profiles" / "github" / safe_id(account["id"])
    assert_private_path(profile)
    env.update(GH_CONFIG_DIR=str(profile), GH_HOST="github.com", GH_PROMPT_DISABLED="1",
               GIT_TERMINAL_PROMPT="0")
    return env


def github_author(account: dict) -> tuple[str, str]:
    """The commit identity saved on a GitHub account ("" when not set); rejects values Git would mangle."""
    name, email = account.get("author_name", "") or "", account.get("author_email", "") or ""
    for value in (name, email):
        if not isinstance(value, str) or len(value) > 320 or any(ord(c) < 32 for c in value) or any(c in value for c in "<>"):
            raise ValueError("Invalid Git author")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+", email):
        raise ValueError(tr('Git author email format is invalid'))
    return name.strip(), email.strip()


def github_session_env(account: dict, root: Path, parent: dict | None = None) -> dict:
    """Environment for work launched with a bound GitHub account.

    Pushes to github.com use that account's official gh helper and commits use its identity, whatever the
    repository or global Git configuration says. Only the processes Launcher starts are affected.
    """
    env = github_env(account, root, parent)
    env.update(GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="credential.https://github.com.helper", GIT_CONFIG_VALUE_0="",
               GIT_CONFIG_KEY_1="credential.https://github.com.helper", GIT_CONFIG_VALUE_1=github_helper(env))
    name, email = github_author(account)
    if name and email:
        env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
    return env


def github_whoami(env: dict) -> dict:
    """Login, numeric id and display name of the account the profile is signed in to ({} when signed out)."""
    result = github_run(["gh", "api", "user"], env, allowed=(0, 1))
    if result.returncode:
        return {}
    try:
        data = json.loads(result.stdout[:65536])
    except ValueError:
        raise RuntimeError(tr('GitHub returned invalid account data')) from None
    login, user_id = data.get("login") if isinstance(data, dict) else None, data.get("id") if isinstance(data, dict) else None
    if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login) or type(user_id) is not int:
        raise RuntimeError(tr('GitHub returned an invalid username'))
    name = data.get("name") if isinstance(data.get("name"), str) else ""
    name = "".join(c for c in name if ord(c) >= 32 and c not in "<>")[:120].strip()
    return {"login": login, "user_id": str(user_id), "display_name": name,
            # GitHub links commits made with this address to the account without exposing a real email.
            "noreply_email": f"{user_id}+{login}@users.noreply.github.com"}


def github_url(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", value):
        raise ValueError(tr('Use a credential-free https://github.com/owner/repository URL'))
    owner, repo = value[len("https://github.com/"):].split("/")
    if owner in {".", ".."} or repo in {".", "..", ".git"}:
        raise ValueError("Invalid GitHub repository")
    return value


def github_repository_url(value: str) -> str:
    """GitHub identity for read-only inspection; never changes the repository remote."""
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value[len("git@github.com:"):]
    elif value.startswith("ssh://git@github.com/"):
        value = "https://github.com/" + value[len("ssh://git@github.com/"):]
    return github_url(value)


def github_run(args: list[str], env: dict, cwd: Path | None = None, *, allowed=(0,)):
    result = subprocess.run([trusted_executable(args[0]), *args[1:]], env=env, cwd=cwd,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if result.returncode not in allowed:
        # Neither stderr nor commands are safe diagnostic material (remote URLs may contain credentials).
        raise RuntimeError(tr('GitHub operation failed; check sign-in, repository permissions, and network connectivity'))
    return result


def github_config(env: dict, path: Path, key: str) -> list[str]:
    result = github_run(["git", "config", "--local", "--null", "--get-all", key], env, path, allowed=(0, 1))
    return result.stdout.split("\0")[:-1] if result.returncode == 0 else []


def github_helper(env: dict) -> str:
    executable = trusted_executable("gh").replace("\\", "/")
    directory = env["GH_CONFIG_DIR"].replace("\\", "/")
    return ("!env -u GH_TOKEN -u GITHUB_TOKEN -u GH_ENTERPRISE_TOKEN -u GITHUB_ENTERPRISE_TOKEN "
            "-u GH_REPO -u GH_HOST GH_HOST=github.com GH_CONFIG_DIR=" + shlex.quote(directory)
            + " " + shlex.quote(executable) + " auth git-credential")


def github_inspect(path: Path, env: dict, remote: str = "", *, check_access: bool = False) -> dict:
    assert_private_path(path)
    if not path.is_dir():
        raise ValueError(tr('Folder does not exist'))
    probe = github_run(["git", "rev-parse", "--show-toplevel"], env, path, allowed=(0, 128))
    if probe.returncode:
        # Not a Git repository yet: the account can still be associated; remotes and settings come after `git init`.
        return {"root": str(path), "git": False, "remotes": [], "branch": "", "author": {}, "access": "unchecked"}
    root = probe.stdout.strip()
    path = Path(root)
    assert_private_path(path)
    remotes = []
    for name in github_run(["git", "remote"], env, path).stdout.splitlines():
        url = github_run(["git", "remote", "get-url", "--", name], env, path).stdout.strip()
        try:
            github_repository_url(url)
            safe_url = url
        except ValueError:
            safe_url = tr('[URL unsupported or containing credentials]')
        remotes.append({"name": name, "url": safe_url})
    author = {}
    for key in ("user.name", "user.email"):
        result = github_run(["git", "config", "--show-origin", "--get", key], env, path, allowed=(0, 1))
        source, separator, value = result.stdout.rstrip("\n").partition("\t")
        author[key] = {"value": value if separator else "", "source": source if separator else ""}
    access = "unchecked"
    if check_access:
        selected = next((item for item in remotes if item["name"] == remote), None)
        if not selected:
            raise ValueError(tr('Select a repository remote'))
        url = github_repository_url(selected["url"])
        slug = url[len("https://github.com/"):].removesuffix(".git")
        result = github_run(["gh", "api", "repos/" + slug, "--jq", ".full_name"], env, allowed=(0, 1))
        access = "accessible" if result.returncode == 0 else "unavailable"
    branch = github_run(["git", "branch", "--show-current"], env, path).stdout.strip()
    return {"root": root, "git": True, "remotes": remotes, "branch": branch, "author": author, "access": access}


def github_manifest(manifest_path: Path, path: Path, account: dict) -> dict:
    """Load and validate a binding record before anything is changed with it."""
    manifest = json.loads(read_private(manifest_path))
    if not isinstance(manifest, dict) or manifest.get("root") != str(path) or not isinstance(manifest.get("changes"), dict):
        raise ValueError("Invalid binding manifest")
    if manifest["account_id"] != account["id"]:
        raise ValueError(tr('Repository is bound to another GitHub account'))
    for key, entry in manifest["changes"].items():
        if key not in {"credential.https://github.com.helper", "user.name", "user.email"} and not re.fullmatch(r"remote\.[A-Za-z0-9_][A-Za-z0-9_./-]*\.(url|fetch)", key):
            raise ValueError("Invalid binding manifest")
        if not isinstance(entry, dict) or any(not isinstance(entry.get(field), list) or
                any(not isinstance(value, str) or "\0" in value for value in entry[field])
                for field in ("before", "after") + (("managed",) if "managed" in entry else ())):
            raise ValueError("Invalid binding manifest")
    return manifest


def github_action(request: dict, root: Path) -> dict:
    account = request["github_account"]
    environment = request.get("environment_id") or "local"
    if (account.get("environment_id") or "local") != environment:
        raise ValueError(tr('GitHub account does not belong to this environment'))
    operation = request.get("github_operation")
    if operation not in {"status", "login", "inspect", "clone", "init", "bind", "unbind"}:
        raise ValueError("Unsupported GitHub operation")
    env = github_env(account, root)
    if operation in {"status", "login"}:
        if operation == "login":
            private_dir(Path(env["GH_CONFIG_DIR"]))
            login_env = dict(env)
            login_env.pop("GH_PROMPT_DISABLED", None)
            # gh may offer to configure Git credentials during its interactive flow.
            # Redirect that official operation away from the user's global Git configuration.
            login_config = Path(env["GH_CONFIG_DIR"]) / "login.gitconfig"
            assert_private_path(login_config)
            login_env.update(GIT_CONFIG_GLOBAL=str(login_config), GIT_CONFIG_NOSYSTEM="1")
            print(tr('GitHub sign-in is handled by the official gh CLI. Note its credential store warnings; Launcher cannot verify the storage method.'), flush=True)
            # The device flow authorizes whichever account the browser is signed in to, which is the usual
            # reason a second login "comes back as the same account".
            print(tr('Note: the browser authorizes the account currently signed in to github.com. To use another account, switch accounts in the upper-right corner of github.com first, or paste the URL below into an incognito window, then enter the one-time code again.'), flush=True)
            code = subprocess.call([trusted_executable("gh"), "auth", "login", "--hostname", "github.com",
                                    "--git-protocol", "https", "--web"], env=login_env,
                                   cwd=Path(env["GH_CONFIG_DIR"]))
            if code:
                return {"status": "failed", "credential_storage": "unverified"}
        identity = github_whoami(env)
        return {"status": "authenticated" if identity else "unavailable", "login": identity.get("login", ""),
                **identity, "credential_storage": "unverified"}
    raw_path = request.get("path", "")
    if not isinstance(raw_path, str) or not raw_path or any(ord(char) < 32 for char in raw_path):
        raise ValueError("Invalid project path")
    path = Path(raw_path).expanduser().absolute()
    assert_private_path(path)
    if operation == "clone":
        url = github_url(request.get("github_url", ""))
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise ValueError(tr('The clone destination must not exist or must be an empty folder'))
        helper = github_helper(env)
        github_run(["git", "-c", "credential.https://github.com.helper=",
                    "-c", "credential.https://github.com.helper=" + helper, "clone", "--", url, str(path)], env)
        return dict(github_inspect(path, env), status="cloned")
    if operation == "init":
        if not path.is_dir():
            raise ValueError(tr('Folder does not exist'))
        if github_inspect(path, env)["git"]:
            raise ValueError(tr('This folder is already a Git repository'))
        github_run(["git", "init"], env, path)
        return dict(github_inspect(path, env), status="initialized")
    info = github_inspect(path, env, request.get("github_remote", ""),
                          check_access=operation == "inspect" and request.get("github_check_access") is True)
    if operation == "inspect":
        return info
    if not info["git"]:
        if operation == "unbind":
            return {"status": "unbound", "preserved": []}
        raise ValueError(tr('This folder is not a Git repository; run git init first, then apply the GitHub settings'))
    path = Path(info["root"])
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
    manifest_path = root / "github-bindings" / (digest + ".json")
    assert_private_path(manifest_path)
    if operation == "unbind":
        if not manifest_path.exists():
            return {"status": "unbound", "preserved": []}
        manifest = github_manifest(manifest_path, path, account)
        preserved = []
        for key, entry in manifest["changes"].items():
            if github_config(env, path, key) != entry.get("managed", entry["after"]):
                preserved.append(key)
                continue
            github_run(["git", "config", "--local", "--unset-all", key], env, path, allowed=(0, 5))
            for value in entry["before"]:
                github_run(["git", "config", "--local", "--add", key, value], env, path)
        manifest_path.unlink()
        return {"status": "unbound", "preserved": preserved}
    # The remote is optional: an account can be bound first and the GitHub remote added later.
    remote = request.get("github_remote", "")
    if not isinstance(remote, str) or (remote and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", remote)):
        raise ValueError(tr('Select a valid remote'))
    values = {"credential.https://github.com.helper": ["", github_helper(env)]}
    url = ""
    selected = next((item for item in info["remotes"] if item["name"] == remote), None)
    if request.get("github_new_remote") is True:
        if not remote or selected:
            raise ValueError(tr('Remote already exists; select it directly'))
        url = github_url(request.get("github_url", ""))
        values["remote." + remote + ".url"] = [url]
        values["remote." + remote + ".fetch"] = ["+refs/heads/*:refs/remotes/" + remote + "/*"]
    elif remote:
        if not selected or selected["url"] != request.get("github_expected_remote_url"):
            raise ValueError(tr('Remote changed; check again'))
        url = github_url(request.get("github_url") or selected["url"])
        if url != selected["url"]:
            values["remote." + remote + ".url"] = [url]
    elif request.get("github_url"):
        raise ValueError(tr('Select a remote to add or update'))
    for field, key in (("github_author_name", "user.name"), ("github_author_email", "user.email")):
        if field in request and request[field]:
            value = request[field]
            if not isinstance(value, str) or len(value) > 320 or any(ord(c) < 32 for c in value):
                raise ValueError("Invalid Git author")
            values[key] = [value]
    changes = {key: {"before": github_config(env, path, key), "after": value} for key, value in values.items()}
    revision = hashlib.sha256(json.dumps(changes, sort_keys=True).encode("utf-8")).hexdigest()
    if request.get("github_apply") is not True:
        preview = dict(changes)
        preview["credential.https://github.com.helper"] = {
            "before": [tr('[existing repository helper]') for _ in changes["credential.https://github.com.helper"]["before"]],
            "after": [tr('[disable inheritance helper]'), tr('[official gh helper for the selected GitHub account]')],
        }
        return {"status": "preview", "changes": preview, "root": str(path), "url": url, "remote": remote,
                "revision": revision}
    if request.get("github_revision") != revision:
        raise ValueError(tr('Repository settings changed; preview and confirm again'))
    # Binding again with the same account (e.g. to add the remote later) extends the existing record. Keys it already
    # manages keep their original value, so unbinding still restores what the repository had before Launcher.
    previous = github_manifest(manifest_path, path, account)["changes"] if manifest_path.exists() else {}
    manifest = {"account_id": account["id"], "root": str(path), "changes": dict(previous)}
    for key, entry in changes.items():
        entry["managed"] = list(entry["before"])
        if key in previous:
            entry["before"] = list(previous[key]["before"])
        manifest["changes"][key] = entry
    write_json(manifest_path, manifest)
    for key, entry in changes.items():
        github_run(["git", "config", "--local", "--unset-all", key], env, path, allowed=(0, 5))
        entry["managed"] = []
        write_json(manifest_path, manifest)
        for value in entry["after"]:
            github_run(["git", "config", "--local", "--add", key, value], env, path)
            entry["managed"].append(value)
            write_json(manifest_path, manifest)
    return {"status": "bound", "root": str(path), "url": url, "remote": remote}


def harness_port(value: int) -> int:
    if type(value) is not int or not 1024 <= value <= 65535:
        raise ValueError("Harness port must be between 1024 and 65535")
    return value


def harness_identity(request: dict) -> str:
    account = request.get("account", {})
    environment_id = safe_id(request["environment_id"])
    if account.get("tool") != "deepseek-harness" or account.get("environment_id") != environment_id:
        raise ValueError(tr('The Harness account must be bound to this SSH environment'))
    account_id = safe_id(account["id"])
    github = request.get("github_account")
    github_id = ""
    if github:
        if github.get("environment_id") != environment_id:
            raise ValueError(tr('GitHub account does not belong to this SSH environment'))
        github_id = safe_id(github["id"])
    return hashlib.sha256(json.dumps([account_id, github_id, environment_id]).encode()).hexdigest()[:32]


def harness_env(request: dict, root: Path, parent: dict | None = None) -> dict:
    key = harness_identity(request)
    source = os.environ if parent is None else parent
    # These are the only inherited values written into the nonsecret startup
    # script. Provider/cloud tokens and user Node injection flags never enter it.
    env = {name: value for name, value in source.items() if name in {
        "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "USER", "LOGNAME", "TERM", "SSH_CONNECTION", "SSH_CLIENT",
        "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"}}
    home = root / "profiles" / "deepseek-harness" / key
    assert_private_path(home)
    github = request.get("github_account")
    gh_home = root / "profiles" / "github" / safe_id(github["id"]) if github else home / "github-unbound"
    assert_private_path(gh_home)
    env.update(HOME=str(home), DSH_HOME=str(home), GH_CONFIG_DIR=str(gh_home), GH_HOST="github.com",
               GH_PROMPT_DISABLED="1", GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="1",
               GIT_CONFIG_KEY_0="credential.https://github.com.helper", GIT_CONFIG_VALUE_0="")
    if github:
        env.update(GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_1="credential.https://github.com.helper",
                   GIT_CONFIG_VALUE_1=github_helper(env))
    return env


def harness_prepare(home: Path) -> None:
    private_dir(home)
    patch = home / "cordis.patch.yml"
    assert_private_path(patch)
    # Never read or overwrite the official profile once initialized.
    if not patch.exists():
        with patch.open("x", encoding="utf-8") as stream:
            protect_path(patch)
            stream.write("- id: session-log-deepseek\n  config:\n    enabled: false\n"
                         "- id: session-telemetry-otel\n  config:\n    mode: DISABLED\n")


def harness_tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([trusted_executable("tmux"), "-L", "vibe-launcher", *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          encoding="utf-8", errors="replace", timeout=10)


def harness_pane(session: str, token: str) -> int | None:
    check = harness_tmux("has-session", "-t", "=" + session)
    if check.returncode:
        # has-session's nonzero code alone cannot distinguish missing server
        # from an inaccessible socket; list-sessions confirms the former.
        listing = harness_tmux("list-sessions", "-F", "#{session_name}")
        if listing.returncode and "no server running" not in listing.stderr and "no such file" not in listing.stderr.lower():
            raise RuntimeError(tr('Harness tmux status could not be confirmed'))
        if session in listing.stdout.splitlines():
            raise RuntimeError(tr('Harness tmux status mismatch'))
        return None
    owned = harness_tmux("show-options", "-v", "-t", "=" + session + ":", "@vibe-harness-owner")
    panes = harness_tmux("list-panes", "-t", "=" + session, "-F", "#{pane_pid}")
    if owned.returncode or owned.stdout.strip() != token or panes.returncode or not re.fullmatch(r"\d+", panes.stdout.strip()):
        raise RuntimeError(tr('Harness service ownership mismatch; refusing to connect or stop'))
    return int(panes.stdout.strip())


def harness_listener_owned(port: int, pane: int) -> bool | None:
    """None means no listener, True an owned listener, False an unknown one.

    Linux /proc socket inodes are compared with the live tmux pane's process
    tree. Saved PIDs alone never authorize connection or termination.
    """
    proc = Path("/proc")
    if not (proc / "net/tcp").is_file():
        raise RuntimeError(tr('The experimental Harness ownership check requires Linux /proc'))
    inodes = set()
    for name in ("tcp", "tcp6"):
        path = proc / "net" / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 9 and fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                # Reject wildcard/IPv6 listeners: this product owns IPv4 loopback.
                if fields[1].split(":")[0] != "0100007F":
                    return False
                inodes.add(fields[9])
    if not inodes:
        return None
    if pane <= 0:
        return False  # a listener without a live owned tmux pane is unknown
    parents = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            parents[int(entry.name)] = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue  # processes exiting during this snapshot are harmless
    descendants = {pane}
    while True:
        expanded = descendants | {pid for pid, parent in parents.items() if parent in descendants}
        if expanded == descendants:
            break
        descendants = expanded
    found = set()
    for pid in descendants:
        try:
            for fd in (proc / str(pid) / "fd").iterdir():
                try:
                    link = os.readlink(fd)
                except FileNotFoundError:
                    continue
                if link.startswith("socket:["):
                    found.add(link[8:-1])
        except FileNotFoundError:
            continue
    return inodes <= found


def harness_action(request: dict, root: Path) -> dict:
    if WINDOWS:
        raise ValueError(tr('The Harness Web service supports SSH Linux hosts only'))
    import socket
    import fcntl
    key = harness_identity(request)
    operation = request.get("harness_operation")
    if operation not in {"start", "status", "stop"}:
        raise ValueError("Unsupported Harness operation")
    port = harness_port(request.get("harness_port", 3080))
    local_port = harness_port(request.get("local_port", port))
    directory = root / "web-services" / key
    private_dir(directory)
    lock_path = directory / "service.lock"
    assert_private_path(lock_path)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(lock_path, flags, 0o600), "r+") as lock:
        protect_path(lock_path)
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(tr('Harness service is busy; try again later'))
                time.sleep(0.05)
        manifest = directory / "service.json"
        assert_private_path(manifest)
        record = json.loads(read_private(manifest)) if manifest.exists() else None
        session = "harness-" + key
        if record and (record.get("service_key") != key or not re.fullmatch(r"[a-f0-9]{32}", record.get("token", ""))):
            raise ValueError("Invalid Harness service record")
        token = record["token"] if record else uuid.uuid4().hex
        pane = harness_pane(session, token)
        if pane and (not record or record["port"] != port or record["local_port"] != local_port):
            raise ValueError(tr('Cannot change the port while the service is running; stop and restart it'))
        result = {"service_key": key, "status": "stopped", "port": port, "local_port": local_port,
                  "account_id": request["account"]["id"], "github_account_id": (request.get("github_account") or {}).get("id", ""),
                  "environment_id": request["environment_id"], "experimental": True}
        if pane:
            owned = harness_listener_owned(port, pane)
            if owned is False:
                raise RuntimeError(tr('Connection port belongs to an unknown service; refusing to connect'))
            result["status"] = "running" if owned else "starting"
            if operation != "stop":
                return result
            stopped = harness_tmux("kill-session", "-t", "=" + session)
            if stopped.returncode or harness_pane(session, token) is not None:
                raise RuntimeError(tr('Harness stop was not confirmed'))
            deadline = time.monotonic() + 3
            while harness_listener_owned(port, pane) is not None:
                if time.monotonic() >= deadline:
                    raise RuntimeError(tr('Harness stop timed out; a service is still using the remote port'))
                time.sleep(0.05)
            result["status"] = "stopped"
            return result
        if harness_listener_owned(port, 0) is not None:
            result["status"] = "unknown"
            if operation == "start":
                raise RuntimeError(tr('Remote port belongs to an unknown service; refusing to start or connect'))
            return result
        if operation != "start":
            return result
        for dependency in ("node", "dsh", "tmux"):
            trusted_executable(dependency)
        # Check without adopting or terminating any existing listener.
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(tr('Remote connection port is already in use')) from exc
        env = harness_env(request, root)
        home = Path(env["DSH_HOME"])
        harness_prepare(home)
        private_dir(Path(env["GH_CONFIG_DIR"]))
        command = [trusted_executable("dsh"), "web", "--host", "127.0.0.1", "--port", str(port),
                   "--trusted-host", f"127.0.0.1:{local_port}", "--no-open"]
        # cwd is the isolated home, preventing accidental project .env imports.
        script = directory / "start.sh"
        assert_private_path(script)
        with script.open("w", encoding="utf-8") as stream:
            protect_path(script)
            stream.write("#!/bin/sh\nexec " + shlex.join([trusted_executable("env"), "-i", *[name + "=" + value for name, value in env.items()], *command]) + "\n")
        write_json(manifest, {"service_key": key, "token": token, "port": port, "local_port": local_port})
        started = harness_tmux("new-session", "-d", "-s", session, "-c", str(home), shlex.join(["/bin/sh", str(script)]))
        if started.returncode:
            raise RuntimeError(tr('Harness tmux service failed to start'))
        tagged = harness_tmux("set-option", "-t", "=" + session + ":", "@vibe-harness-owner", token)
        if tagged.returncode:
            raise RuntimeError(tr('Harness service started but ownership marking failed; check tmux manually'))
        pane = harness_pane(session, token)
        if pane is None:
            raise RuntimeError(tr('Harness exited after startup; use the official diagnostics to check version compatibility'))
        owned = harness_listener_owned(port, pane)
        if owned is False:
            raise RuntimeError(tr('Remote connection port ownership mismatch'))
        result["status"] = "running" if owned else "starting"
        return result


if __name__ == "__main__":
    raise SystemExit(main())
