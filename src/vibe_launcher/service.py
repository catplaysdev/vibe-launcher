"""GUI-facing launcher: create nonsecret requests and open an external terminal."""
from __future__ import annotations

from .i18n import tr

import base64
import ast
import hashlib
import json
import lzma
import os
import re
import shlex
import subprocess
import sys
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from . import runtime
from . import selfcmd, ssh_security
from .store import Store


def data_root() -> Path:
    override = os.environ.get("VIBE_LAUNCHER_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "VibeLauncher"


# Conversation copying and extension sharing are only reached from launches and the import actions; the GitHub and
# Harness helpers stay under the SSH command length limit without them.
HISTORY_ONLY = {"_copy_transcripts", "sync_history", "_same_project_path", "_codex_rollout_cwd", "claude_project_folder",
                "sync_project_history", "sync_user_data", "native_codex_state",
                "sync_extensions", "_shareable_files", "shared_settings", "shared_codex_config", "toml_dumps", "_toml_key",
                "_toml_value", "_write_private", "_merge_shared_settings", "_retain_missing", "manual_import",
                "user_plugin_dirs", "read_user_text", "resumable"}

# GitHub helpers every feature needs: work launched for a bound project runs with that account's identity.
SHARED_GITHUB = {"github_env", "github_session_env", "github_author", "github_helper"}

# Each remote helper ships only the code its action reaches, so every command stays under the Windows command-line
# limit. SSH hosts are never Windows. tests/test_runtime.py runs every feature's own path on the shipped
# module, so a missing definition fails there and not on a server.
WINDOWS_ONLY = {"windows_secret", "windows_user_sid", "windows_process_alive"}
# The managed Codex terminal: the launcher-owned app-server bridge, its controller and the quota monitor.
MANAGED_ONLY = {"QuotaSelection", "_quota_timestamp", "quota_state", "select_quota_account", "commit_quota_cursor",
                "flow_outcome", "initial_quota_account", "checked_quota_policy", "quota_flow", "quota_template", "flow_issues",
                "flow_link_issue", "claude_handoff_lock", "claude_handoff_locked",
                "managed_codex_command", "managed_codex_supported", "codex_profile_fingerprint", "_instruction_identity",
                "instruction_sources_match", "observed_quota", "CodexQuotaMonitor", "ManagedCodexSession",
                "thread_owner_active", "stop_process", "run_managed_codex", "CodexControlError", "ManagedCodexBridge", "CodexHandoffBlocked",
                "codex_rule_differences", "align_codex_rules", "account_in_use", "_skill_inventory", "_enabled_plugins", "_callable_apps",
                "claude_hook", "write_claude_event", "record_claude_quota", "record_claude_status", "_git_bash", "run_user_statusline", "claude_settings",
                "claude_user_statusline", "managed_claude_supported",
                "latest_claude_session", "_copy_private_tree", "copy_claude_session", "carry_claude_onboarding", "claude_profile_fingerprint",
                "claude_capabilities", "ClaudeSupervisor", "ManagedClaudeSession", "run_managed_claude"}
# Constants only the managed terminal reads (Claude hooks, block workflows, the Codex bridge).
MANAGED_CONSTANTS = ("CLAUDE_", "FLOW_", "_BRIDGE_")
# Shared by the managed terminal and the control requests that reach it.
QUOTA_FLOW = {"QuotaPolicy", "validate_control_accounts", "validate_codex_control", "codex_control_path",
              "control_worker_alive", "control_heartbeat_age", "process_alive"}
CONTROL_ONLY = {"codex_control_status", "codex_control_action"}
# Tool maintenance, session administration and folder browsing: never reached by a launch or its worker.
NOT_LAUNCH = {"detach_session", "manage_tools", "update_route", "inspect_tool", "upstream_version", "tool_output", "version_number",
              "maintenance_env", "list_sessions", "session_action", "stop_direct_session", "browse_listing", "manual_import"}
# A control request only reads the worker's state and queues a command for it.
CONTROL_KEEP = QUOTA_FLOW | CONTROL_ONLY | {
    "main", "perform", "emit", "now", "tr", "operation_result", "validate_request", "validate_endpoint", "validate_terminal_profile",
    "safe_id", "profile_dir", "account_fits", "quota_supported", "switch_supported", "assert_private_path", "assert_private_paths",
    "protect_path", "private_dir", "read_private", "write_json", "pid_alive", "augmented_path", "user_tool_dirs",
    "_safe_user_dir", "_node_version"}
QUOTA_KEEP = CONTROL_KEEP | {"prepare", "isolated_env", "api_key", "trusted_executable", "fetch_quota",
                             "quota_file", "quota_summary", "rpc_rate_limits", "rpc_request", "rpc_requests"}


def _shipped(name: str, feature: str) -> bool:
    if name == "detach_session":
        return feature == "session"
    if name in WINDOWS_ONLY:
        return False
    if feature == "quota":
        return name in QUOTA_KEEP
    if feature == "control":
        return name in CONTROL_KEEP
    if name.startswith("harness_"):
        return feature == "harness"
    if name.startswith("github_") and name not in SHARED_GITHUB:
        return feature == "github"
    if name in HISTORY_ONLY and feature in {"github", "harness"}:
        return False
    if name in MANAGED_ONLY or name in QUOTA_FLOW or name in CONTROL_ONLY:
        return feature == "managed" and name not in CONTROL_ONLY
    return not (feature == "managed" and name in NOT_LAUNCH)


def _shipped_node(node: ast.stmt, feature: str) -> bool:
    """Does this top-level statement of runtime.py travel with the feature's helper?"""
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        return _shipped(node.name, feature)
    return feature == "managed" or not (isinstance(node, ast.Assign) and all(
        isinstance(target, ast.Name) and target.id.startswith(MANAGED_CONSTANTS) for target in node.targets))


def remote_feature(request: dict) -> str:
    """Which helper a request needs (see ``_shipped``)."""
    if request["action"] == "detach":
        return "session"
    if request["action"] == "quota":
        return "quota"
    if request["action"] in {"github", "harness"}:
        return request["action"]
    if request.get("worker_kind") == "harness":
        return "harness"
    if request["action"] == "codex-control":
        return "control"
    if (request["action"] in {"launch", "worker"} and request.get("mode", "agent") != "shell"
            and runtime.switch_supported(request.get("account") or {})):
        return "managed"
    return "cli"


@lru_cache(maxsize=8)
def _compressed_runtime(source: bytes, feature: str = "cli", locale: str = "en") -> tuple[str, str]:
    # Ship the same module without comments/docstrings; no remote package dependency.
    tree = ast.parse(source)
    tree.body = [node for node in tree.body if _shipped_node(node, feature)]
    from .i18n import catalog
    # Bake only the selected language into the standalone helper. Shipping both
    # source strings and a second catalog can exceed Windows' SSH command limit.
    messages = catalog(locale)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr"
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            key = node.args[0].value
            node.args[0].value = messages.get(key, key)
    for node in ast.walk(tree):
        # Remote execution uses validation functions, not type annotations.
        # Omit hints to leave room for translated messages and job metadata.
        if isinstance(node, ast.arg):
            node.annotation = None
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.returns = None
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
            node.body.pop(0)
            if not node.body:
                node.body.append(ast.Pass())
    shipped = ast.unparse(tree).encode("utf-8")
    return hashlib.sha256(shipped).hexdigest(), base64.b85encode(lzma.compress(shipped)).decode("ascii")


WIDE_BASE = 0x4E00          # CJK Unified Ideographs: 2**14 consecutive assigned, printable characters


def _wide(data: bytes) -> str:
    """14 bits per character. Windows limits a command line in UTF-16 units, not bytes, so a helper too large for ASCII
    still fits; OpenSSH sends the command to the host as UTF-8 (three bytes each, far below the host's limits)."""
    padded = data + bytes(-len(data) % 7)
    out = []
    for start in range(0, len(padded), 7):
        value = int.from_bytes(padded[start:start + 7], "big")
        out.append("".join(chr(WIDE_BASE + (value >> shift & 0x3FFF)) for shift in (42, 28, 14, 0)))
    return "".join(out)


def remote_command(request: dict) -> str:
    """Single SSH authentication; deploy content-addressed helper without SCP.

    Only shipped Python code and validated nonsecret metadata are embedded. Base64
    is transport quoting, not encryption. OpenSSH supplies encryption and host trust.
    """
    runtime.validate_request(request)
    source = selfcmd.runtime_source()
    digest, compressed = _compressed_runtime(source, remote_feature(request), request.get("language", "en"))
    request_json = json.dumps(request).encode("utf-8")
    payload = "'" + base64.urlsafe_b64encode(request_json).decode("ascii") + "'"

    def bootstrap(load: str) -> str:
        return ("import base64,lzma,pathlib,os,sys,hashlib,uuid;" + load +
                # A helper damaged in transit never runs: 78 tells the launcher that nothing was started.
                f"sys.exit(78) if hashlib.sha256(b).hexdigest()!='{digest}' else None;"
                "d=pathlib.Path.home()/'.local/share/vibe-launcher/helpers';"
                f"p=d/'runtime-{digest}.py';"
                "import types;m=types.ModuleType('vibe_bootstrap');sys.modules[m.__name__]=m;"
                "ns=m.__dict__;ns['__file__']=str(p);exec(compile(b,str(p),'exec'),ns);"
                "ns['private_dir'](d);ns['assert_private_path'](p);"
                "t=d/('upload-'+uuid.uuid4().hex+'.tmp');"
                "f=t.open('xb');os.chmod(t,0o600);f.write(b);f.close();t.replace(p);"
                f"os.execv(sys.executable,[sys.executable,'-I',str(p),{payload}])")

    command = shlex.join(["/usr/bin/python3", "-I", "-c", bootstrap(f"b=lzma.decompress(base64.b85decode('{compressed}'));")])
    if len(command) > 28000:
        # Too long as ASCII: carry the helper as a separate argument, 14 bits per character, followed by the request
        # (compressed: account pools and flows repeat themselves). The host recovers the exact bytes with os.fsencode
        # whatever its locale, and the digest check above still guards the helper.
        raw, packed = base64.b85decode(compressed), lzma.compress(request_json)
        load = ("c=[ord(x)-19968 for x in os.fsencode(sys.argv[1]).decode()];"
                "a=b''.join((c[i]<<42|c[i+1]<<28|c[i+2]<<14|c[i+3]).to_bytes(7,'big') for i in range(0,len(c),4));"
                f"b=lzma.decompress(a[:{len(raw)}]);"
                f"q=base64.urlsafe_b64encode(lzma.decompress(a[{len(raw)}:{len(raw) + len(packed)}])).decode();")
        payload = "q"
        command = shlex.join(["/usr/bin/python3", "-I", "-c", bootstrap(load), _wide(raw + packed)])
    if len(command) > 28000 or len(command.encode("utf-8")) > 120000:
        raise ValueError(tr('service.80d4e1d496b1'))
    return command


def ssh_args(target: str, request: dict, *, root: Path | None = None, environment: dict | None = None) -> list[str]:
    if not re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*", target) or target.startswith("-"):
        raise ValueError("Invalid SSH target; use an SSH config alias")
    root = root or data_root()
    environment = environment or {"id": request.get("environment_id", "default"), "ssh_target": target}
    args = ssh_security.base_args(root, environment)
    settings = ssh_security.resolve(root, environment)
    mode = environment.get("ssh_auth_mode", "interactive")
    if mode == "password":
        args += ["-o", "PreferredAuthentications=password", "-o", "KbdInteractiveAuthentication=no", "-o", "PubkeyAuthentication=no"]
    elif mode == "key":
        args += ["-o", "PreferredAuthentications=publickey", "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no"]
    noninteractive = request["action"] in {"detach", "stop", "codex-control", "list-sessions", "quota", "browse", "harness", "import-settings", "import-history"} or (request["action"] == "github" and request.get("github_operation") != "login")
    args += ["-T" if noninteractive else "-tt", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]
    if request.get("login_method") == "browser" and request.get("account", {}).get("tool") == "codex":
        # Re-evaluate with forwarding enabled; refuse any inherited forwards.
        result = subprocess.run(ssh_security.base_args(root, environment) + ["-o", "ClearAllForwardings=no", "-G", target],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        if result.returncode or any(line.startswith(("localforward ", "remoteforward ", "dynamicforward ")) for line in result.stdout.splitlines()):
            raise ValueError("Browser callback requires an SSH alias without configured forwards")
        args += ["-o", "ClearAllForwardings=no", "-o", "ExitOnForwardFailure=yes", "-L", "127.0.0.1:1455:127.0.0.1:1455"]
    return args + [target, remote_command(request)]


class LauncherService:
    supports_spawn = True   # start(..., spawn=callable) can run the job in the app's built-in terminal

    def __init__(self, store: Store):
        self.store = store
        self.root = store.path.parent

    def control_accounts(self, accounts: list, environment_id: str) -> list[dict]:
        """The Codex subscription accounts a control request may name, re-read from the store: only their nonsecret
        identity fields leave the desktop, whatever the caller passed in."""
        if not isinstance(accounts, list) or len(accounts) > 128:
            raise ValueError("Invalid Codex account pool")
        result = []
        for item in accounts:
            account = self.store.get("accounts", item.get("id") if isinstance(item, dict) else item)
            if not runtime.switch_supported(account) or not runtime.account_fits(account, environment_id):
                raise ValueError("Select Codex or Claude subscription accounts of this environment")
            result.append({key: account[key] for key in runtime.CONTROL_ACCOUNT_FIELDS if key in account})
        return result

    def start(self, action: str, *, preset_id: str | None = None, account_id: str | None = None,
              environment_id: str | None = None, session_id: str | None = None,
              login_method: str = "device", tool: str | None = None, snap_channel: str = "",
              mode: str = "agent", spawn: Callable[[list[str], dict], Any] | None = None,
              session_ids: list[str] | None = None, operation: dict | None = None,
              github_account_id: str | None = None, terminal_profile: str = "", resume: bool = False,
              resume_thread_id: str = "", quota_policy: dict | None = None, workspace_id: str = "",
              quota_cursor: str = "") -> str | None:
        """Create a job and run it. ``spawn(args, context)`` runs it in a terminal of the caller's choosing (the app's
        built-in terminal); without it the job opens in an external console window. Returns the new session id."""
        if action not in {"detach", "codex-control", "launch", "login", "doctor", "refresh", "attach", "stop", "ssh-settings", "check-tools", "update-tool", "quota", "browse", "github", "harness", "import-settings", "import-history"}:
            raise ValueError("Unknown action")
        request = {"action": "list-sessions" if action == "refresh" else action,
                   "nonce": uuid.uuid4().hex, "login_method": login_method}
        from .i18n import language
        request["language"] = language()
        if operation is not None:
            operation = dict(operation)
            allowed = ({"attachment_nonce"} if action == "detach" else
                       {"control_action", "target_account_id", "allow_gaps", "align_rules", "policy", "accounts"} if action == "codex-control" else
                       {"github_operation", "github_remote", "github_url", "github_apply", "github_check_access", "github_expected_remote_url", "github_revision", "github_author_name", "github_author_email", "path", "github_new_remote", "harness_operation", "harness_port", "local_port", "project_history"})
            if action not in {"detach", "codex-control", "github", "harness", "import-settings", "import-history"} or operation.keys() - allowed:
                raise ValueError("Unsupported operation parameters")
            request.update(operation)
        if action in {"detach", "codex-control", "login", "github", "harness", "import-settings", "import-history"}:
            request["operation_id"] = uuid.uuid4().hex
        if action in {"check-tools", "update-tool"}:
            request.update(tool=tool, snap_channel=snap_channel)
        if action == "ssh-settings":
            request.update(action="doctor", local_diagnostics_only=True)
        if preset_id:
            preset = self.store.get("presets", preset_id)
            # Explicit account/environment (e.g. "open this project with Claude" or "on another host") win over the saved ones.
            if action == "launch" and environment_id and environment_id != preset["environment_id"]:
                host = self.store.get("environments", preset["environment_id"]).get("name", preset["environment_id"])
                raise ValueError(tr('service.146d63e774e8', v0=f"{preset.get('name')}", v1=f'{host}'))
            account_id, environment_id = account_id or preset.get("account_id") or "", environment_id or preset["environment_id"]
            request.update(path=preset["path"], persistent=bool(preset.get("persistent", True)))
        if session_ids:
            if action != "stop":
                raise ValueError("Several sessions can only be stopped together")
            found = [self.store.get("sessions", sid) for sid in session_ids]
            if len({s["environment_id"] for s in found}) != 1:
                raise ValueError("Select sessions from one environment at a time")
            session_id, account_id = found[0]["id"], None      # no single account: each session keeps its own
            environment_id = found[0]["environment_id"]
            request.update(session_ids=[s["id"] for s in found], session_id=session_id)
        elif session_id:
            session = self.store.get("sessions", session_id)
            environment_id = session["environment_id"]
            account_id = session["account_id"]
            request["session_id"] = session_id
        if account_id and not environment_id:
            # Logging in / asking a quota question needs no environment from the caller: it is wherever the login lives.
            environment_id = self.store.get("accounts", account_id).get("environment_id") or ""
        environment_id = environment_id or "local"
        environment = self.store.get("environments", environment_id)
        request["environment_id"] = environment_id
        if github_account_id is None and preset_id:
            github_account_id = preset.get("github_account_id")
        if github_account_id:
            github = self.store.get("github_accounts", github_account_id)
            if github["environment_id"] != environment_id:
                raise ValueError(tr('service.23e42e32ad67'))
            request["github_account"] = github
        if action == "harness" and environment["kind"] != "ssh":
            raise ValueError(tr('service.a5d7e87f354f'))
        if mode not in {"agent", "shell"} or (mode == "shell" and action != "launch"):
            raise ValueError("Unsupported session mode")
        if account_id:
            account = self.store.get("accounts", account_id)
            if mode != "shell":  # a plain shell session needs no account (the id is only kept for the record)
                if action in {"launch", "login", "quota"} and not runtime.account_fits(account, environment_id):
                    place = self.store.get("environments", account["environment_id"]).get("name", account["environment_id"])
                    raise ValueError(tr('service.7a9ded1746a1', v0=f"{account.get('name')}", v1=f'{place}'))
                request["account"] = account
        if action == "quota" and not runtime.quota_supported(request.get("account", {})):
            raise ValueError("Quota lookup is only available for Codex subscription accounts")
        if mode == "shell":
            request["mode"] = "shell"
            from .settings import selected_profile
            selected = selected_profile(self.root, environment, terminal_profile)
            if selected is not None:
                request["terminal_profile"] = selected
        elif terminal_profile:
            raise ValueError("Terminal profile requires terminal mode")
        if resume and action == "launch" and mode != "shell":
            request["resume"] = True        # continue the folder's last conversation (restoring, not a fresh start)
        if resume_thread_id:
            if action != "launch" or not runtime.switch_supported(request.get("account", {})):
                raise ValueError("Exact conversation resume requires a Codex or Claude subscription launch")
            request["resume_thread_id"] = runtime.safe_id(resume_thread_id)
        if action == "codex-control":
            request["accounts"] = self.control_accounts(request.get("accounts", []), environment_id)
        if workspace_id and action in {"launch", "stop"} and environment["kind"] == "ssh":
            request["workspace_id"] = runtime.safe_id(workspace_id)     # the workspace's tmux session on the host
        if (action == "launch" and mode != "shell" and environment["kind"] != "ssh"
                and request.get("account", {}).get("tool") == "claude" and runtime.switch_supported(request["account"])):
            # Hooks of a managed local Claude session call back into the launcher (a host uses its own runtime.py).
            request["claude_hook_command"] = selfcmd.hook_command("claude_hook")
        if quota_policy is not None:
            if action != "launch" or mode == "shell" or not runtime.switch_supported(request.get("account", {})):
                raise ValueError("Quota management requires a Codex or Claude subscription launch")
            policy = runtime.checked_quota_policy(quota_policy)
            request.update(quota_policy=policy.to_dict(),
                           quota_accounts=self.control_accounts(list(policy.account_ids), environment_id))
            if quota_cursor in policy.account_ids:
                request["quota_cursor"] = quota_cursor
        if action in {"launch", "login", "quota"} and not account_id and mode != "shell":
            raise ValueError("Select an account")
        if action == "launch" and not preset_id:
            raise ValueError("Select a project")
        if action in {"detach", "stop", "attach"} and not session_id:
            raise ValueError("Select a session")
        runtime.validate_request(request)
        if environment["kind"] == "ssh":
            remote_command(request)  # Reject an oversized job before creating a session or opening a terminal.
        if action == "launch":
            session = self.store.save("sessions", {
                "id": uuid.uuid4().hex, "preset_id": preset_id, "account_id": account_id or "",
                "environment_id": environment_id, "name": preset["name"], "path": preset["path"],
                "persistent": request["persistent"], "mode": mode, "status": "queued", "updated_at": runtime.now(),
                **({"terminal_profile": request["terminal_profile"]["id"]} if "terminal_profile" in request else {}),
                **({"workspace_id": request["workspace_id"]} if request.get("workspace_id") else {}),
            })
            request["session_id"] = session["id"]
            preset["last_used"] = runtime.now()
            if mode != "shell" and account_id and environment_id == preset["environment_id"]:
                preset["account_id"] = account_id          # remembered as the one to suggest next time
            self.store.save("presets", preset)
        job = {"request": request, "environment": environment, "database": str(self.store.path.resolve())}
        jobs = self.root / "jobs"
        runtime.private_dir(jobs)
        job_path = jobs / (uuid.uuid4().hex + ".json")
        runtime.write_json(job_path, job)
        args = selfcmd.command("runner", str(job_path.resolve()))
        try:
            if spawn is not None:
                spawn(args, {"action": action, "mode": session.get("mode", "agent") if action == "attach" else mode, "session_id": request.get("session_id"),
                             "environment_id": environment_id, "preset_id": preset_id, "account_id": account_id,
                             "operation_id": request.get("operation_id", ""), "attachment_nonce": request["nonce"],
                             "terminal_profile": session.get("terminal_profile", "") if action == "attach" else request.get("terminal_profile", {}).get("id", ""),
                             "terminal_label": request.get("terminal_profile", {}).get("label", ""),
                             "login_operation": action == "login" or (action == "github" and request.get("github_operation") == "login"),
                             **({"operation_title": "GitHub · " + request.get("github_operation", ""), "operation_account_name": request["github_account"]["name"]}
                                if action == "github" else {})})
            else:
                if os.name != "nt":
                    raise RuntimeError("Desktop launcher currently requires Windows")
                subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)
        except Exception:
            job_path.unlink(missing_ok=True)
            if action == "launch":
                session["status"] = "failed"
                self.store.save("sessions", session)
            raise
        return request.get("operation_id") or request.get("session_id")
