"""Before a quota plan is saved or applied: can its accounts take over each other's conversations?

The worker checks this at every handoff (ManagedCodexSession / ManagedClaudeSession.validate_target). Here the same
comparisons run ahead of time, for every account a plan may switch between, on this computer (the accounts of an SSH
host are checked on the host, at the handoff):

* environment layer: shared settings, instruction and extension files, and (Codex) approval rules must be the same on
  every account; ``align`` makes an account match the one the user chose as reference, after a backup;
* account layer: plugins, connected apps and MCP sign-ins belong to each account and cannot be copied. The user installs
  or signs in there and checks again, or accepts switching without them (QuotaPolicy.accepted_gaps); anything that
  appears later is not accepted, so the worker stops before such a switch.

Credentials are never read: the CLIs report their own state, and files are compared by hash."""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import runtime
from .i18n import tr

ALIGNABLE = ("settings", "rules")       # environment layer: the reference's version can be copied over
SKIPPED_FOLDERS = ("plugins", "rules")  # plugin caches are account-level; rules are compared on their own


@dataclass(frozen=True)
class Finding:
    account_id: str
    kind: str                           # settings | rules | login | error, or one of runtime.GAP_KINDS
    name: str
    holders: tuple = ()                 # a missing capability: the accounts that have it

    @property
    def key(self) -> str:
        return self.kind + ":" + self.name


@dataclass
class Report:
    reference: str
    accounts: tuple
    findings: list = field(default_factory=list)
    signature: dict = field(default_factory=dict)

    def open(self, accepted: dict | None) -> list:
        """What still stands in the way: everything except the account-level gaps the plan accepted."""
        accepted = accepted or {}
        return [finding for finding in self.findings
                if not (finding.kind in runtime.GAP_KINDS and finding.key in accepted.get(finding.account_id, ()))]


def _fingerprint(account: dict, root: Path) -> dict:
    if account["tool"] == "codex":
        return runtime.codex_profile_fingerprint(account, root)
    # The model of a Claude conversation is passed by flag at a handoff, so the profiles' default model may differ.
    return runtime.claude_profile_fingerprint(account, root, ("model",))


def _differing(first: dict, second: dict) -> list:
    """The same grouping the worker reports (at most three path parts)."""
    return sorted({"/".join(Path(key).parts[:3]) for key in set(first) | set(second) if first.get(key) != second.get(key)})


def _capabilities(account: dict, root: Path, project: Path) -> dict:
    """{"plugin": {id: version}, "app": {id: name}, "mcp": {name: status or transport}} of one account."""
    profile = runtime.profile_dir(root, account)
    if account["tool"] == "claude":
        plugins, servers = runtime.claude_capabilities(profile, project)
        return {"plugin": {name: True for name in plugins}, "app": {}, "mcp": dict(servers)}
    calls = [("plugin/installed", {"cwds": [str(project)]}), ("app/installed", {}),
             ("mcpServerStatus/list", {"detail": "toolsAndAuthOnly", "limit": 100})]
    plugins, apps, servers = runtime.rpc_requests([runtime.trusted_executable("codex"), "app-server"],
                                                  runtime.prepare(account, root), calls, profile, timeout=60)
    status = {}
    for server in servers.get("data") or [] if isinstance(servers, dict) else []:
        if isinstance(server, dict) and isinstance(server.get("name"), str):
            status[server["name"][:64]] = server.get("authStatus")
    return {"plugin": runtime._enabled_plugins(plugins), "app": runtime._callable_apps(apps), "mcp": status}


def _gaps(tool: str, target: str, capabilities: dict) -> list:
    """What switching to ``target`` from any other account may lose, named exactly as the worker names it."""
    own, others = capabilities[target], {key: value for key, value in capabilities.items() if key != target}
    found = {}

    def add(kind: str, name: str, holder: str) -> None:
        found.setdefault((kind, str(name)[:64]), set()).add(holder)

    for holder, theirs in others.items():
        for identifier, version in theirs["plugin"].items():
            if own["plugin"].get(identifier) != version:
                add("plugin", identifier.split("@")[0], holder)
        for identifier, name in theirs["app"].items():
            if identifier not in own["app"]:
                add("app", name, holder)
        for name, state in theirs["mcp"].items():
            if tool == "claude":
                if name not in own["mcp"]:
                    add("mcp", name, holder)
                elif state != "stdio":
                    add("mcp_unknown", name, holder)        # a remote sign-in lives in credentials, never read
            elif state == "unknown":
                add("mcp_unknown", name, holder)            # Codex itself cannot tell
            elif state == "oAuth" and own["mcp"].get(name) != "oAuth":
                add("mcp", name, holder)
    return [Finding(target, kind, name, tuple(sorted(holders))) for (kind, name), holders in sorted(found.items())]


def signature(accounts: list, root: Path) -> dict:
    """A cheap stamp of what ``check`` reads in each profile (names, sizes and times; the MCP part of Claude's state):
    when it changes, check again."""
    result = {}
    for account in accounts:
        profile = runtime.profile_dir(root, account)
        parts = []
        try:
            for relative in runtime._shareable_files(profile, account["tool"]):
                stat = (profile / relative).stat()
                parts.append([relative.as_posix(), stat.st_size, stat.st_mtime_ns])
            for name in ("settings.json", "config.toml"):
                if (profile / name).is_file():
                    stat = (profile / name).stat()
                    parts.append([name, stat.st_size, stat.st_mtime_ns])
            for path in sorted((profile / "rules").glob("*.rules")) if (profile / "rules").is_dir() else []:
                parts.append(["rules/" + path.name, path.stat().st_size, path.stat().st_mtime_ns])
            if account["tool"] == "claude":
                # .claude.json changes all the time while Claude runs; only its MCP servers matter here.
                parts.append(["mcp", sorted(runtime.claude_capabilities(profile, Path("/"))[1].items())])
        except (OSError, ValueError):
            parts.append(["unreadable"])
        result[account["id"]] = hashlib.sha256(json.dumps(parts).encode()).hexdigest()
    return result


def check(accounts: list, reference_id: str, root: Path, project: Path, *, refresh: bool = True) -> Report:
    """Compare every account with the reference (environment layer) and with each other (account layer). ``refresh``
    first brings the user's own extensions into every profile, as each launch does."""
    ids = tuple(account["id"] for account in accounts)
    report = Report(reference_id if reference_id in ids else (ids[0] if ids else ""), ids)
    usable = []
    for account in accounts:
        if refresh:
            try:
                runtime.sync_extensions(account, root, follow_source=True)
            except Exception:
                report.findings.append(Finding(account["id"], "error", "extensions"))
                continue
        try:
            signed_in = runtime.check_login(account, root, runtime.prepare(account, root))
        except (OSError, ValueError, subprocess.SubprocessError):
            signed_in = False
        if not signed_in:
            report.findings.append(Finding(account["id"], "login", "sign-in"))
            continue
        usable.append(account)
    prints = {}
    for account in usable:
        try:
            prints[account["id"]] = _fingerprint(account, root)
        except (OSError, ValueError):
            report.findings.append(Finding(account["id"], "error", "settings"))
    reference = report.reference if report.reference in prints else next(iter(prints), "")
    by_id = {account["id"]: account for account in usable}
    for identifier, values in prints.items():
        if identifier == reference:
            continue
        report.findings += [Finding(identifier, "settings", name) for name in _differing(prints[reference], values)]
        if by_id[identifier]["tool"] == "codex":
            try:
                names = runtime.codex_rule_differences(runtime.profile_dir(root, by_id[reference]),
                                                       runtime.profile_dir(root, by_id[identifier]))
            except (OSError, ValueError, RuntimeError):
                names = ["*"]
            report.findings += [Finding(identifier, "rules", name) for name in names]
    capabilities = {}
    for account in usable:
        try:
            capabilities[account["id"]] = _capabilities(account, root, project)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            report.findings.append(Finding(account["id"], "error", "capabilities"))
    for identifier in capabilities:
        report.findings += _gaps(by_id[identifier]["tool"], identifier, capabilities)
    report.signature = signature(accounts, root)
    return report


# --- alignment: the user chose an account whose environment layer the others should have ---------------------------
def _manifest(profile: Path) -> dict:
    try:
        data = json.loads(runtime.read_private(profile / runtime.SHARE_MANIFEST))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _replace(path: Path, data: bytes | None, backup: Path, name: str) -> bool:
    """Write (or with None remove) one target file, after copying what it had under ``backup``."""
    current = runtime.read_private_bytes(path) if path.is_file() else None
    if current == data:
        return False
    if current is not None:
        runtime._write_private(backup / name, current)
    if data is None:
        runtime.assert_private_path(path)
        path.unlink()
    else:
        runtime._write_private(path, data)
    return True


def align(target: dict, reference: dict, root: Path, session_id: str = "") -> list:
    """Give ``target`` the environment layer of ``reference``: approval rules, extension and instruction files, and the
    shared part of its settings. Each file keeps the reference's sync record, so the next launch treats it as it treats
    the reference's (following the user's own settings, or kept as a personal edit). Every target file that changes is
    first copied to alignment-backups/<target>/<time>/. Sign-ins, plugins, trusted folders and the target's default Claude
    model are never touched. Refused while another terminal runs the target. Returns what changed."""
    tool = target["tool"]
    if reference["tool"] != tool or reference["id"] == target["id"]:
        raise ValueError("Align accounts of the same CLI")
    if runtime.account_in_use(root, target["id"], session_id):
        raise ValueError(tr("{account} is open in another terminal; close it there before aligning it",
                            account=str(target.get("name") or target["id"])[:64]))
    source, destination = runtime.profile_dir(root, reference), runtime.profile_dir(root, target)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = root / "alignment-backups" / runtime.safe_id(target["id"]) / stamp
    changed = []
    if tool == "codex":
        changed += ["rules/" + name for name in runtime.align_codex_rules(source, destination, backup / "rules")]
    theirs, mine = _manifest(source), _manifest(destination)
    files, hashes, stats = set(mine.get("files", [])), dict(mine.get("file_hashes", {})), dict(mine.get("file_stats", {}))
    wanted = {path.as_posix() for path in runtime._shareable_files(source, tool)}
    present = {path.as_posix() for path in runtime._shareable_files(destination, tool)}
    for name in sorted(wanted | present):
        if name.split("/")[0] in SKIPPED_FOLDERS:
            continue
        data = runtime.read_private_bytes(source / name) if name in wanted else None
        if _replace(destination / name, data, backup, name):
            changed.append(name)
        # the reference's record: launcher copy (follows the user's files), personal edit, or personal deletion
        (files.add if name in theirs.get("files", []) else files.discard)(name)
        if name in theirs.get("file_hashes", {}):
            hashes[name] = theirs["file_hashes"][name]
        else:
            hashes.pop(name, None)
        stats.pop(name, None)
    if tool == "claude":
        changed += _align_claude_settings(source, destination, backup)
        mine["settings_snapshot"] = {key: value for key, value in theirs.get("settings_snapshot", {}).items() if key != "model"}
        mine["env_snapshot"] = dict(theirs.get("env_snapshot", {}))
    else:
        changed += _align_codex_config(source, destination, backup)
    mine.update(files=sorted(files), file_hashes=hashes, file_stats=stats)
    runtime._write_private(destination / runtime.SHARE_MANIFEST, json.dumps(mine).encode("utf-8"))
    return changed


def _load_json(path: Path) -> dict:
    data = json.loads(runtime.read_private(path)) if path.is_file() else {}
    if not isinstance(data, dict):
        raise ValueError("Expected a settings object")
    return data


def _align_claude_settings(source: Path, destination: Path, backup: Path) -> list:
    reference, current = _load_json(source / "settings.json"), _load_json(destination / "settings.json")
    shared, env = runtime.shared_settings(reference)
    _, own_env = runtime.shared_settings(current)
    result = {key: value for key, value in current.items() if key not in runtime.SHARE_SETTINGS or key == "model"}
    result.update({key: value for key, value in shared.items() if key != "model"})
    merged = {key: value for key, value in (current.get("env") if isinstance(current.get("env"), dict) else {}).items()
              if key not in own_env}
    merged.update(env)
    if merged:
        result["env"] = merged
    else:
        result.pop("env", None)
    if result == current:
        return []
    data = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
    return ["settings.json"] if _replace(destination / "settings.json", data, backup, "settings.json") else []


def _align_codex_config(source: Path, destination: Path, backup: Path) -> list:
    import tomllib
    reference_text = runtime.read_private(source / "config.toml") if (source / "config.toml").is_file() else ""
    current_text = runtime.read_private(destination / "config.toml") if (destination / "config.toml").is_file() else ""
    reference, current = tomllib.loads(reference_text), tomllib.loads(current_text)
    shared = set(runtime.SHARE_TOML_KEYS) | set(runtime.SHARE_TOML_TABLES)
    result = {key: value for key, value in current.items() if key not in shared}
    result.update({key: reference[key] for key in runtime.SHARE_TOML_KEYS if key in reference})
    result.update({key: reference[key] for key in runtime.SHARE_TOML_TABLES if isinstance(reference.get(key), dict)})
    text = runtime.toml_dumps(result)
    if runtime.shared_codex_config(text) == runtime.shared_codex_config(current_text):
        return []
    return ["config.toml"] if _replace(destination / "config.toml", text.encode("utf-8"), backup, "config.toml") else []
