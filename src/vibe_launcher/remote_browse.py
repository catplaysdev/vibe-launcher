"""List folders on an SSH host without opening a terminal, so the project dialog can offer a folder picker for it.

Uses the same managed OpenSSH policy (pinned host keys, one account per environment) as every other connection.
Passwords / OTP / host-key questions are asked in a small window by the askpass bridge, never cached unless the
environment is already set to remember its password. Nothing but folder names comes back.
"""
from __future__ import annotations

from .i18n import tr

import json
import subprocess
import uuid
from pathlib import Path
from typing import Callable

from . import askpass, runtime
from .service import ssh_args


class RemoteBrowseError(RuntimeError):
    """The listing could not be fetched; the message is safe to show."""


def _auth_environment(environment: dict, root: Path, database: Path) -> dict | None:
    """Askpass settings for a background connection: always a window to answer in, since there is no terminal.

    An environment that does not remember its password still gets a (non-saving) prompt window for this call.
    """
    if environment.get("remember_seconds", 0):
        return askpass.ssh_environment(environment, root, database)
    probe = {**environment, "remember_seconds": 900, "ssh_auth_mode": "interactive"}
    return askpass.ssh_environment(probe, root, database)


def list_remote(environment: dict, path: str, root: Path, database: Path, *, timeout: int = 120,
                run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    """``{"path", "requested", "exists", "home", "parent", "dirs", "truncated"}`` for ``path`` on the environment's host.

    A path that does not exist falls back to its nearest existing parent (``exists`` is then False).
    """
    if environment.get("kind") != "ssh" or not environment.get("ssh_target"):
        raise RemoteBrowseError(tr('remote_browse.30da9e03e793'))
    request = {"action": "browse", "nonce": uuid.uuid4().hex, "environment_id": environment["id"], "path": path or "~"}
    try:
        args = ssh_args(environment["ssh_target"], request, root=root, environment=environment)
    except (ValueError, RuntimeError) as error:
        raise RemoteBrowseError(str(error)) from error
    env = _auth_environment(environment, root, database)
    marker = "VIBE_BROWSE_" + request["nonce"] + ":"
    creation = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    cancelled = False
    try:
        result = run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                     env=env, creationflags=creation)
        cancelled = env is not None and askpass.was_cancelled(env)
    except subprocess.TimeoutExpired as error:
        raise RemoteBrowseError(tr('remote_browse.d25250aa4fcb')) from error
    except OSError as error:
        raise RemoteBrowseError(tr('remote_browse.750ccb50856b', v0=f'{error.strerror or type(error).__name__}')) from error
    finally:
        askpass.cleanup(env)
    out = (result.stdout or b"").decode("utf-8", "replace")
    for line in out.splitlines():
        if line.startswith(marker):
            try:
                data = json.loads(line[len(marker):])
            except ValueError as error:
                raise RemoteBrowseError(tr('remote_browse.949eb45de1f0')) from error
            if not isinstance(data, dict) or not isinstance(data.get("dirs"), list):
                raise RemoteBrowseError(tr('remote_browse.949eb45de1f0'))
            return data
    if cancelled:
        raise RemoteBrowseError(tr('remote_browse.97863301e761'))
    detail = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    hint = detail[-1][:200] if detail else tr('remote_browse.6b0b75a7b423', v0=f'{result.returncode}')
    raise RemoteBrowseError(tr('remote_browse.b686cc3fec4d', v0=f'{hint}'))
