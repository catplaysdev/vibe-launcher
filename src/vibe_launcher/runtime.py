"""Standalone stdlib runtime, also deployed verbatim to the SSH host.

No credentials travel in requests or status events. Only this process and the
official CLIs touch the dedicated profile credentials.
"""
from __future__ import annotations

import base64
import ctypes
import getpass
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
import unicodedata
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
    if request.get("action") not in {"doctor", "login", "launch", "list-sessions", "attach", "stop", "worker", "check-tools", "update-tool", "quota", "browse", "github", "harness", "import-settings", "import-history"}:
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

    Searched *after* the system PATH, so system installs win. Only directories owned by the current user and not
    group/world-writable qualify, which keeps another account's or a shared directory's binary from being picked.
    """
    if WINDOWS:
        return []
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
                resume: bool = False) -> list[str]:
    command = build_cli(account, action, root, method, project, resume)
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
              resume: bool = False) -> list[str]:
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
    if resume and resumable(account, root, project):
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
    print("\nVIBE_EVENT_" + request.get("nonce", "") + ":" + json.dumps(event), flush=True)


# --- sharing the user's own skills / hooks / agents with an isolated profile -------------------------------------
# A profile has its own CLAUDE_CONFIG_DIR / CODEX_HOME, so the user's ~/.claude and ~/.codex are invisible to it.
# We copy (never link: launcher data may not contain links) only the *extension* parts, never credentials.
# This file is also run on SSH hosts as a single script, so everything it needs lives here.
SHARE_DIRS = {"claude": ("skills", "agents", "commands", "hooks", "output-styles"),
              "codex": ("skills", "prompts", "agents", "rules", "plugins")}
SHARE_FILES = {"claude": (), "codex": ("hooks.json",)}      # plus every top-level *.md (CLAUDE.md, AGENTS.md, files they @import)
SHARE_SETTINGS = ("model", "hooks", "permissions", "statusLine", "outputStyle", "theme", "effortLevel", "attribution")
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
            dirs[:] = sorted(d for d in dirs if (linked_skills or _plain(here / d))
                             and d not in {".git", "node_modules", "__pycache__", ".system"})
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
    return subprocess.run([trusted_executable("tmux"), "-L", "vibe-launcher", *args],
                          env={**os.environ, "LC_ALL": "C"},
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def tmux_name(session_id: str) -> str:
    return "vibe-" + safe_id(session_id)


def tmux_session_exists(session_id: str) -> bool:
    result = tmux("has-session", "-t", "=" + tmux_name(session_id), capture=True)
    if result.returncode == 0:
        return True
    error = (result.stderr or b"").strip()
    absent = (error.startswith(b"can't find session:") or error.startswith(b"no server running on ")
              or (error.startswith(b"error connecting to ") and error.endswith(b"(No such file or directory)")))
    if result.returncode == 1 and absent:
        return False
    raise RuntimeError("Cannot verify tmux session state; check socket access before retrying")


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
            result = tmux("has-session", "-t", tmux_name(record["session_id"]), capture=True)
            if result.returncode == 0:
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
        command = shell_command(profile) if shell and profile else shell_command() if shell else cli_command(request["account"], "launch", root, "device",
                                                                                                          Path(request["path"]), request.get("resume") is True)
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
        name = "=" + tmux_name(request["session_id"])
        if tmux_session_exists(request["session_id"]):
            result = tmux("kill-session", "-t", name)
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
    name = "=" + tmux_name(request["session_id"])
    result = tmux("attach-session" if action == "attach" else "kill-session", "-t", name)
    if action == "stop" and result.returncode == 0:
        save_status(root, record, "ended")
        emit(request, {"session_id": request["session_id"], "status": "ended"})
    if action == "attach" and result.returncode == 0:
        return session_outcome(request, root, 0)
    return result.returncode


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
    if not WINDOWS:
        # Make CLIs installed under the user's home discoverable (and their `node`) without asking users to edit PATH.
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
    if action == "browse":
        print("VIBE_BROWSE_" + request.get("nonce", "") + ":" + json.dumps(browse_listing(request.get("path", ""))), flush=True)
        return 0
    if action == "quota":
        summary = fetch_quota(request["account"], root, request.get("environment_id", "local"))
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
    command = shlex.join(["/usr/bin/env", "PATH=" + os.environ.get("PATH", ""), sys.executable, "-I", str(Path(__file__).resolve()), "--worker-file",
                          str(session_path(root, request["session_id"]))])
    name = tmux_name(request["session_id"])
    result = tmux("new-session", "-d", "-s", name, "-c", request["path"], command)
    if result.returncode:
        raise RuntimeError("Cannot create tmux session")
    global _worker_created
    _worker_created = True
    tmux("set-option", "-t", "=" + name, "prefix2", "C-b", capture=True)  # best effort: lets the app send "detach"
    # tmux then publishes "vibe:<session>" as the terminal title, which is how the app knows it is really attached.
    tmux("set-option", "-t", "=" + name, "set-titles", "on", capture=True)
    tmux("set-option", "-t", "=" + name, "set-titles-string", "vibe:#S", capture=True)
    if stop_path.exists():
        result = tmux("kill-session", "-t", "=" + name)
        if result.returncode:
            raise RuntimeError("Stop requested; cannot confirm tmux worker was stopped")
        save_status(root, request, "ended")
        return 0
    emit(request, {"session_id": request["session_id"], "status": "running"})
    result = tmux("attach-session", "-t", name)
    return session_outcome(request, root, result.returncode)


# --- account quota (Codex subscription: the 5-hour and weekly windows) --------------------------------------------
def quota_file(root: Path, account_id: str, environment_id: str) -> Path:
    return root / "quota" / (safe_id(account_id) + "__" + safe_id(environment_id or "local") + ".json")


def quota_supported(account: dict) -> bool:
    """Only a Codex subscription has a quota to ask about; an API key / endpoint is billed per use."""
    return account.get("tool") == "codex" and account.get("auth") == "subscription"


def rpc_rate_limits(command: list[str], env: dict, cwd: Path | None = None, timeout: float = 30.0) -> dict:
    """Ask `codex app-server` (the CLI's own JSON-RPC interface) for the account's rate limits.

    Codex itself talks to its backend with the profile's login; this launcher never reads or sends the token.
    """
    import queue
    import threading
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
        send({"id": 2, "method": "account/rateLimits/read", "params": None})
        return answer(2)
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
        if isinstance(item, dict) and isinstance(item.get("usedPercent"), (int, float)) and not isinstance(item.get("usedPercent"), bool):
            minutes, resets = item.get("windowDurationMins"), item.get("resetsAt")
            windows.append({"slot": slot, "used_percent": max(0, min(100, item["usedPercent"])),
                            "window_minutes": minutes if isinstance(minutes, int) else None,
                            "resets_at": resets if isinstance(resets, int) else None})
    credits = snapshot.get("credits")
    credits = ({"unlimited": credits.get("unlimited") is True, "has_credits": credits.get("hasCredits") is True,
                "balance": credits.get("balance") if isinstance(credits.get("balance"), str) else None}
               if isinstance(credits, dict) else None)
    resets = result.get("rateLimitResetCredits") if isinstance(result, dict) else None
    count = resets.get("availableCount") if isinstance(resets, dict) else None
    plan = snapshot.get("planType")
    reached = snapshot.get("rateLimitReachedType")
    return {"tool": "codex", "plan": plan if isinstance(plan, str) else None, "windows": windows, "credits": credits,
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
    owned = harness_tmux("show-options", "-v", "-t", "=" + session, "@vibe-harness-owner")
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
        tagged = harness_tmux("set-option", "-t", "=" + session, "@vibe-harness-owner", token)
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
