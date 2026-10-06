"""OpenSSH policy shared by launch, password prompts and diagnostics."""
from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from . import runtime


def executable(name: str = "ssh") -> str:
    if os.name == "nt":
        buffer = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer)):
            raise RuntimeError("Cannot locate Windows system directory")
        path = Path(buffer.value) / "OpenSSH" / (name + ".exe")
        if not path.is_file():
            raise RuntimeError("Install Windows OpenSSH Client optional feature")
        return str(path)
    search = os.pathsep.join(p for p in os.get_exec_path() if Path(p).is_absolute())
    found = shutil.which(name, path=search)
    if not found:
        raise RuntimeError("OpenSSH client is missing")
    return str(Path(found).resolve())


def policy_file(root: Path, environment: dict) -> Path:
    directory = root / "ssh" / runtime.safe_id(environment["id"])
    runtime.private_dir(directory)
    known = directory / "known_hosts"
    runtime.assert_private_path(known)
    if not known.exists():
        with known.open("x", encoding="utf-8"):
            pass
    runtime.protect_path(known)
    # -F is inherited by OpenSSH's implicit ProxyJump command, protecting jump hosts too.
    config = directory / "config"
    user_config = Path.home() / ".ssh" / "config"
    text = "Host *\n" + "\n".join("    " + line for line in (
        "StrictHostKeyChecking ask", f'UserKnownHostsFile "{known.as_posix()}"',
        "GlobalKnownHostsFile none", "KnownHostsCommand none", "VerifyHostKeyDNS no",
        "UpdateHostKeys no", "ForwardAgent no", "ForwardX11 no", "ForwardX11Trusted no",
        "ControlMaster no", "ControlPath none", "PermitLocalCommand no", "RemoteCommand none", "CanonicalizeHostname no",
        "EscapeChar none", "ClearAllForwardings yes")) + "\n"
    if user_config.is_file():
        text += f'Include "{user_config.as_posix()}"\n'
    runtime.assert_private_path(config)
    temporary = directory / ("config-" + __import__("uuid").uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            runtime.protect_path(temporary)
            stream.write(text)
        temporary.replace(config)
    finally:
        temporary.unlink(missing_ok=True)
    return config


def base_args(root: Path, environment: dict) -> list[str]:
    return [executable(), "-F", str(policy_file(root, environment))]


def resolve(root: Path, environment: dict) -> dict:
    result = subprocess.run(base_args(root, environment) + ["-G", environment["ssh_target"]],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", timeout=10)
    if result.returncode:
        raise RuntimeError("Cannot resolve SSH settings")
    settings = {"identityfiles": []}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        settings.setdefault(key, value)
        if key == "identityfile":
            settings["identityfiles"].append(value)
    if settings.get("proxycommand", "none") != "none" and settings.get("proxyjump", "none") == "none":
        raise ValueError("Custom ProxyCommand needs manual review; use ProxyJump for managed connections")
    return settings


def host_binding(root: Path, environment: dict, settings: dict) -> str | None:
    # Only keys accepted by OpenSSH's host verification enter this dedicated file.
    known = root / "ssh" / runtime.safe_id(environment["id"]) / "known_hosts"
    runtime.assert_private_path(known)
    host = settings.get("hostkeyalias")
    if not host or host == "none":
        host = settings["hostname"]
    port = settings.get("port", "22")
    lookup = host if port == "22" else f"[{host}]:{port}"
    result = subprocess.run([executable("ssh-keygen"), "-F", lookup, "-f", str(known)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    if result.returncode:
        return None
    keys = sorted(line for line in result.stdout.splitlines() if line and not line.startswith(b"#"))
    if not keys:
        return None
    digest = hashlib.sha256(b"\n".join(keys)).hexdigest()
    return f'{settings["user"]}@{settings["hostname"]}:{port}:{digest}'
