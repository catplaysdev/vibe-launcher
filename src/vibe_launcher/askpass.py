"""Opt-in Windows SSH askpass; credentials stay in Credential Manager."""
from __future__ import annotations

from .i18n import tr

import ctypes
import base64
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .runtime import safe_id, windows_secret
from . import runtime, selfcmd, ssh_security

POLICIES = [(tr('askpass.3f73cecac6ee'), 0), (tr('askpass.201fad361530'), 900), (tr('askpass.3835ef01ce98'), 3600),
            (tr('askpass.a957c5407fac'), 28800), (tr('askpass.671ea88cfadf'), -1)]


@contextmanager
def security_lock(root: Path, environment_id: str):
    runtime.private_dir(root)
    path = root / "ssh-security.sqlite3"
    runtime.assert_private_path(path)
    connection = sqlite3.connect(path, timeout=10)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS epochs (id TEXT PRIMARY KEY, epoch TEXT NOT NULL)")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT OR IGNORE INTO epochs VALUES (?, ?)", (safe_id(environment_id), uuid.uuid4().hex))
        epoch = connection.execute("SELECT epoch FROM epochs WHERE id=?", (environment_id,)).fetchone()[0]
        yield connection, epoch
        connection.commit()
    finally:
        connection.close()
        runtime.protect_path(path)


def revoke_password(environment_id: str, root: Path) -> None:
    with security_lock(root, environment_id) as (connection, _):
        connection.execute("UPDATE epochs SET epoch=? WHERE id=?", (uuid.uuid4().hex, environment_id))
        delete_secret({"tool": "ssh", "id": environment_id})


def security_status(environment: dict, root: Path) -> dict:
    known = root / "ssh" / safe_id(environment["id"]) / "known_hosts"
    try:
        client = ssh_security.executable()
    except RuntimeError:
        client = tr('askpass.5491c7372beb')
    fingerprints = []
    if known.is_file():
        for line in runtime.read_private(known).splitlines():
            parts = line.split()
            if len(parts) >= 3 and not line.startswith(("#", "@")):
                try:
                    fingerprints.append("SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(parts[2], validate=True)).digest()).decode().rstrip("="))
                except ValueError:
                    continue
    diagnostic = root / "diagnostics" / (safe_id(environment["id"]) + ".json")
    checked = json.loads(runtime.read_private(diagnostic)) if diagnostic.exists() else {}
    return {"trust_status": tr('askpass.32496992289e') if fingerprints else tr('askpass.737755a7f9ef'),
            "auth_method": environment.get("ssh_auth_mode", "interactive"), "ssh_version": client,
            "fingerprint": "\n".join(fingerprints),
            "identityfiles": checked.get("identityfiles", []), "proxyjump": checked.get("proxyjump", tr('askpass.808427228913')),
            "agent_status": tr('askpass.88294f5ec795'),
            **({"ssh_version": checked["ssh_version"], "agent_status": checked["agent_status"] + " · " + checked["checked_at"]} if checked else {})}


def current_policy(env: dict, epoch: str) -> tuple[dict, int] | None:
    if epoch != env.get("VIBE_SSH_EPOCH"):
        return None
    path = Path(env["VIBE_SSH_DATABASE"])
    runtime.assert_private_path(path)
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT payload FROM environments WHERE id=?", (env["VIBE_SSH_ID"],)).fetchone()
    if not row:
        return None
    actual = json.loads(row[0])
    if (actual.get("credential_epoch") != env.get("VIBE_SSH_CONFIG_EPOCH")
            or actual.get("ssh_auth_mode", "interactive") != "password"
            or not actual.get("remember_seconds") or env.get("VIBE_SSH_CACHE") != "1"):
        return None
    return actual, actual["remember_seconds"]


def delete_secret(profile: dict) -> None:
    from ctypes import wintypes
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    target = "VibeLauncher/ssh/" + safe_id(profile["id"])
    if not api.CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
        raise OSError("Cannot clear saved SSH password")


def save_password(profile: dict, password: str, binding: str, ttl: int, *, clock: float | None = None) -> None:
    if ttl == 0:
        return
    stamp = time.time() if clock is None else clock
    windows_secret(profile, json.dumps({"password": password, "binding": binding, "saved_at": stamp,
                                        "expires": None if ttl == -1 else stamp + ttl}))


def cached_password(profile: dict, binding: str, ttl: int, *, clock: float | None = None) -> str | None:
    if ttl == 0:
        return None
    raw = windows_secret(profile)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("password"), str) or not data["password"]:
            raise ValueError("Invalid saved password")
        if any(c in data["password"] for c in "\x00\r\n") or not isinstance(data.get("saved_at"), (int, float)) or not math.isfinite(data["saved_at"]):
            raise ValueError("Invalid saved password")
        if data.get("expires") is not None and (not isinstance(data["expires"], (int, float)) or not math.isfinite(data["expires"])):
            raise ValueError("Invalid saved expiry")
        if data["binding"] != binding:
            return None
        stamp = time.time() if clock is None else clock
        expiry = data["expires"]
        if ttl != -1:
            expiry = min(expiry or float("inf"), data["saved_at"] + ttl)
        if stamp < data["saved_at"] or (expiry is not None and stamp >= expiry):
            delete_secret(profile)
            return None
        return data["password"]
    except (ValueError, KeyError, TypeError):
        delete_secret(profile)
        return None


def is_password_prompt(prompt: str, expected: str) -> bool:
    return bool(expected) and prompt.strip() == expected


def is_host_confirmation(prompt: str) -> bool:
    # Some Win32 OpenSSH builds omit SSH_ASKPASS_PROMPT for host-key prompts.
    # This only selects a Yes/No UI; it never accepts a host or accesses a secret.
    return (prompt.startswith("The authenticity of host ") and "key fingerprint is " in prompt
            and "Are you sure you want to continue connecting (yes/no" in prompt)


def cancellation_path(env: dict) -> Path:
    return Path(env["VIBE_SSH_ROOT"]) / "askpass-attempts" / (safe_id(env["VIBE_SSH_ATTEMPT"]) + ".cancelled")


def mark_cancelled(env: dict) -> None:
    runtime.write_json(cancellation_path(env), {"cancelled": True})


def was_cancelled(env: dict | None) -> bool:
    return bool(env and cancellation_path(env).is_file())


def build_bridge(root: Path) -> Path:
    """Native executable required by Win32 OpenSSH; no secret embedded in it."""
    source = Path(__file__).with_name("askpass_bridge.cs")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    directory = root / "helpers"
    runtime.private_dir(directory)
    # Rebuild per connection; do not trust an executable merely because its name
    # contains the source digest. The runner removes it after OpenSSH exits.
    executable = directory / f"askpass-{digest}-{uuid.uuid4().hex}.exe"
    if not executable.exists():
        system = Path(ssh_security.executable()).parents[1]
        compiler = system.parent / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        if not compiler.is_file():
            raise RuntimeError("Windows .NET Framework compiler unavailable; disable password storage to use terminal login")
        temporary = directory / f"askpass-{uuid.uuid4().hex}.exe"
        result = subprocess.run([str(compiler), "/nologo", "/target:winexe", f"/out:{temporary}", str(source)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError("Cannot build SSH password helper; disable password storage to use terminal login")
        runtime.protect_path(temporary)
        temporary.replace(executable)
    return executable


def ssh_environment(environment: dict, root: Path, database: Path | None = None) -> dict | None:
    ttl = environment.get("remember_seconds", 0)
    if ttl == 0:
        return None  # Original terminal behavior, including SSH key/agent.
    settings = ssh_security.resolve(root, environment)
    user, host, port = settings["user"], settings["hostname"], settings.get("port", "22")
    env = dict(os.environ)
    interpreter, helper_args = selfcmd.helper_environment()
    with security_lock(root, environment["id"]) as (_, epoch):
        pass
    env.update(SSH_ASKPASS=str(build_bridge(root)), SSH_ASKPASS_REQUIRE="force", DISPLAY="vibe-launcher",
               VIBE_SSH_PYTHON=interpreter, VIBE_SSH_ARGS=helper_args, VIBE_SSH_ID=environment["id"], VIBE_SSH_TTL=str(ttl),
               VIBE_SSH_EXPECTED=f"{user}@{host}'s password:",
               VIBE_SSH_SETTINGS=json.dumps(settings), VIBE_SSH_ENVIRONMENT=json.dumps(environment),
               VIBE_SSH_EPOCH=epoch, VIBE_SSH_CONFIG_EPOCH=environment.get("credential_epoch", ""),
               VIBE_SSH_DATABASE=str((database or root / "vibe-launcher.sqlite3").resolve()),
               VIBE_SSH_CACHE="1" if environment.get("ssh_auth_mode") == "password" and settings.get("proxyjump", "none") == "none" else "0",
               VIBE_SSH_ROOT=str(root), VIBE_SSH_ATTEMPT=uuid.uuid4().hex)
    return env


def password_operation(env: dict, value: str | None = None, *, promote: bool = False) -> str | None:
    root = Path(env["VIBE_SSH_ROOT"])
    profile = {"tool": "ssh", "id": safe_id(env["VIBE_SSH_ID"])}
    pending = {"tool": "ssh", "id": safe_id(env["VIBE_SSH_ATTEMPT"])}
    with security_lock(root, profile["id"]) as (_, epoch):
        policy = current_policy(env, epoch)
        if not policy:
            return None
        environment, ttl = policy
        binding = ssh_security.host_binding(root, environment, json.loads(env["VIBE_SSH_SETTINGS"]))
        if not binding:
            return None
        # Bind saved vault entries to the revocation epoch as well as the host.
        binding += ":" + epoch + ":" + env["VIBE_SSH_CONFIG_EPOCH"]
        if promote:
            raw = windows_secret(pending)
            if raw and cached_password(pending, binding, ttl):
                windows_secret(profile, raw)
            delete_secret(pending)
            return None
        if value is not None:
            save_password(pending, value, binding, ttl)
            return None
        return cached_password(profile, binding, ttl)


def cleanup(env: dict | None) -> None:
    if env is None:
        return
    delete_secret({"tool": "ssh", "id": safe_id(env["VIBE_SSH_ATTEMPT"])})
    root = Path(env["VIBE_SSH_ROOT"])
    (root / "askpass-attempts" / safe_id(env["VIBE_SSH_ATTEMPT"])).unlink(missing_ok=True)
    cancellation_path(env).unlink(missing_ok=True)
    bridge = Path(env["SSH_ASKPASS"])
    if bridge.parent == root / "helpers" and bridge.name.startswith("askpass-"):
        bridge.unlink(missing_ok=True)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if was_cancelled(dict(os.environ)):
        return 1
    from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit
    from PySide6.QtCore import Qt

    prompt = os.environ.get("VIBE_SSH_PROMPT", "SSH verification")
    profile = {"tool": "ssh", "id": safe_id(os.environ["VIBE_SSH_ID"])}
    ttl = int(os.environ["VIBE_SSH_TTL"])
    password_prompt = os.environ.get("VIBE_SSH_CACHE") == "1" and is_password_prompt(prompt, os.environ["VIBE_SSH_EXPECTED"])
    if os.environ.get("SSH_ASKPASS_PROMPT") == "confirm" or is_host_confirmation(prompt):
        # Host-key trust is never accepted from a password cache.
        from PySide6.QtWidgets import QMessageBox
        app = QApplication.instance() or QApplication([])
        box = QMessageBox()
        box.setWindowTitle(tr('askpass.b24f20ae0230'))
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(prompt + tr('askpass.1580198431fa'))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.button(QMessageBox.StandardButton.Yes).setText(tr('askpass.6a4966017b46'))
        box.button(QMessageBox.StandardButton.No).setText(tr('askpass.cb26a2bfef3d'))
        answer = box.exec()
        if answer != QMessageBox.StandardButton.Yes:
            mark_cancelled(dict(os.environ))
            return 1
        print("yes")
        return 0
    if password_prompt:
        attempts = Path(os.environ["VIBE_SSH_ROOT"]) / "askpass-attempts"
        runtime.private_dir(attempts)
        path = attempts / safe_id(os.environ["VIBE_SSH_ATTEMPT"])
        try:
            with path.open("x"):
                pass
            cached = password_operation(dict(os.environ))
            if cached:
                print(cached)
                return 0
        except FileExistsError:
            delete_secret(profile)  # Rejected password: ask again, don't repeat it.
    app = QApplication.instance() or QApplication([])
    label = prompt + (tr('askpass.167039a1f0a5') if password_prompt else tr('askpass.fa85d825d9e2'))
    dialog = QInputDialog()
    dialog.setWindowTitle(tr('askpass.75a429cade8c'))
    dialog.setLabelText(label)
    dialog.setTextEchoMode(QLineEdit.EchoMode.Password)
    dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    if not dialog.exec():
        mark_cancelled(dict(os.environ))
        return 1
    value = dialog.textValue()
    if not value or "\n" in value or "\r" in value:
        mark_cancelled(dict(os.environ))
        return 1
    if password_prompt:
        password_operation(dict(os.environ), value)
    print(value)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(1)  # Never print a credential or exception traceback.
