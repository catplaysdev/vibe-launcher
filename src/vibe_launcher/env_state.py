"""Read-only view-model for the environments page.

Turns the stored environment record plus the non-secret files the runner already
writes (diagnostics, tool-version reports, known_hosts fingerprints) into a flat
``EnvState`` the UI can render. It never reads credentials: the password cache
window shown in the UI is the *configured policy*, not a live countdown.
"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import runtime

TONE_RANK = {"unknown": 0, "ok": 1, "warn": 2, "danger": 3}
STATUS_TEXT = {"ok": "可用", "warn": "需注意", "danger": "異常", "unknown": "未檢查"}
TOOLS = ("codex", "claude")
TOOL_LABEL = {"codex": "Codex", "claude": "Claude"}
SHORT_POLICY = {900: "15m", 3600: "1h", 28800: "8h", -1: "持續保存"}
AGENT_TEXT = {"keys available": "有可用金鑰", "no keys loaded": "未載入金鑰", "not available": "不可用"}
AUTH_TEXT = {"interactive": "互動驗證", "password": "密碼", "key": "SSH key"}

# Session status -> (label, tone). One mapping so the sessions list, project list and cards agree.
SESSION_STATUS = {
    "running": ("運行中", "ok"),
    "detached": ("背景執行", "info"),
    "connecting": ("連線中", "info"),
    "waiting_auth": ("等待驗證", "warn"),
    "queued": ("排隊中", "unknown"),
    "ended": ("已停止", "unknown"),
    "failed": ("失敗", "danger"),
    "unknown": ("狀態不明", "unknown"),
    "ready": ("就緒", "ok"),
}
RUNNING_STATUSES = {"running", "detached"}
# Statuses that mean "this session may still be alive" (unknown is included: it cannot be ruled out).
LIVE_STATUSES = {"queued", "connecting", "waiting_auth", "running", "detached", "unknown"}


def project_status(preset_id: str, sessions: list[dict]) -> str | None:
    """A project's own status: its newest possibly-live session, else its newest finished one, else None."""
    mine = [s for s in sessions if s.get("preset_id") == preset_id]
    if not mine:
        return None
    mine.sort(key=lambda s: str(s.get("updated_at") or ""), reverse=True)
    live = [s for s in mine if s.get("status") in LIVE_STATUSES]
    return str((live or mine)[0].get("status") or "unknown")


def project_sessions(preset_id: str, sessions: list[dict]) -> list[dict]:
    """Every session of one project: possibly-live ones first, then finished ones, newest first inside each group."""
    mine = [s for s in sessions if s.get("preset_id") == preset_id]
    mine.sort(key=lambda s: str(s.get("updated_at") or ""), reverse=True)
    return [s for s in mine if s.get("status") in LIVE_STATUSES] + [s for s in mine if s.get("status") not in LIVE_STATUSES]


@dataclass(frozen=True)
class RestoreStep:
    action: str            # "attach" (still running on the server) | "relaunch" (stopped: start it again the same way)
    session: dict


def _work_key(session: dict) -> tuple[str, str, str]:
    mode = session.get("mode") or "agent"
    return (session.get("account_id") or "" if mode != "shell" else "", mode, session.get("environment_id") or "")


def _is_finished(session: dict, envs: dict) -> bool:
    local = envs.get(session.get("environment_id"), {}).get("kind") == "local"
    return session.get("status") in {"ended", "failed"} or (session.get("status") == "unknown" and local)


def _restartable_account(session: dict, known: dict) -> bool:
    if (session.get("mode") or "agent") == "shell":
        return True
    account = known.get(session.get("account_id"))
    return bool(account) and runtime.account_fits(account, session.get("environment_id") or "local")


def can_restart(session: dict, environments: list[dict], accounts: list[dict], project_environment: str = "") -> bool:
    """A stopped workspace that can be started again as it was (a project only opens on its own environment)."""
    if project_environment and session.get("environment_id") != project_environment:
        return False
    return _is_finished(session, {e.get("id"): e for e in environments}) and _restartable_account(session, {a.get("id"): a for a in accounts})


def restore_plan(preset_id: str, sessions: list[dict], environments: list[dict], accounts: list[dict],
                 open_ids: set[str] | frozenset[str] = frozenset(), project_environment: str = "") -> list[RestoreStep]:
    """What "bring this project's workspaces back" should do.

    A *workspace* is a (service/account, mode, environment) combination. Running ones on a server are re-attached; for a
    stopped one only the newest record of its combination is started again, and only when nothing of that combination is
    already open or in progress. Workspaces whose account is gone or no longer fits the environment are left out.
    """
    envs = {e.get("id"): e for e in environments}
    known = {a.get("id"): a for a in accounts}
    mine = project_sessions(preset_id, sessions)
    steps: list[RestoreStep] = []
    covered: set[tuple[str, str, str]] = set()
    usable = lambda session: _restartable_account(session, known)  # noqa: E731
    finished = lambda session: _is_finished(session, envs)  # noqa: E731

    for session in mine:                                       # pass 1: everything that is (or may be) alive
        status = session.get("status")
        if finished(session):
            continue
        key = _work_key(session)
        if session.get("id") in open_ids:
            covered.add(key)
        elif status in {"running", "detached", "unknown"}:
            remote = envs.get(session.get("environment_id"), {}).get("kind") != "local"
            if remote and session.get("persistent", True) is not False and usable(session):
                steps.append(RestoreStep("attach", session))
            covered.add(key)
        else:                                                  # queued / connecting / waiting_auth: on its way up
            covered.add(key)
    for session in mine:                                       # pass 2: the newest stopped record of each combination
        if not finished(session) or session.get("id") in open_ids:
            continue
        key = _work_key(session)
        if key in covered or not usable(session) or (project_environment and session.get("environment_id") != project_environment):
            continue
        covered.add(key)
        steps.append(RestoreStep("relaunch", session))
    return steps


def session_badge(status: str) -> tuple[str, str]:
    return SESSION_STATUS.get(status or "unknown", (status or "狀態不明", "unknown"))


def project_badge(preset_id: str, sessions: list[dict]) -> tuple[str, str] | None:
    """Status pill for a project row: its main status, plus how many workspaces are live when there are several."""
    status = project_status(preset_id, sessions)
    if status is None:
        return None
    label, tone = session_badge(status)
    live = sum(1 for s in sessions if s.get("preset_id") == preset_id and s.get("status") in LIVE_STATUSES)
    return (f"{label} ×{live}" if live > 1 else label), tone


@dataclass(frozen=True)
class Chip:
    key: str
    label: str
    tone: str           # ok | warn | danger | unknown
    icon: str
    tip: str = ""


@dataclass(frozen=True)
class ToolReport:
    tool: str
    installed: str = "unknown"
    latest: str = "unknown"
    status: str = "unknown"
    source: str = ""
    path: str = ""
    channel: str = ""
    candidate: str = ""
    publisher: str = ""
    message: str = ""
    checked_at: str = ""
    can_update: bool = False


@dataclass(frozen=True)
class EnvState:
    id: str
    name: str
    kind: str                       # local | ssh
    builtin: bool
    pinned: bool
    target: str
    subtitle: str
    tone: str
    status_text: str
    status_tip: str
    chips: tuple[Chip, ...]
    last_used: str
    auth_mode: str
    remember_seconds: int
    credential: str                 # local | interactive | key | password_off | password_cached | password_persistent
    trust_ok: bool
    fingerprint: str
    identity: tuple[str, ...]
    proxyjump: str
    agent: str
    agent_checked: bool
    diag_checked_at: str
    ssh_version: str
    openssh_missing: bool
    tools: tuple[ToolReport, ...]
    last_failed: bool
    running_sessions: int = 0

    def tool(self, name: str) -> ToolReport | None:
        return next((t for t in self.tools if t.tool == name), None)


# --------------------------------------------------------------------------- helpers


def parse_time(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def format_relative(raw: str, now: datetime | None = None) -> str:
    """Short, human relative time in Traditional Chinese (``12 分鐘前``)."""
    dt = parse_time(raw)
    if dt is None:
        return "尚未使用" if not raw else str(raw)
    now = now or datetime.now(timezone.utc)
    seconds = (now - dt).total_seconds()
    if seconds < 0:
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    if seconds < 60:
        return "剛剛"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分鐘前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小時前"
    days = int(seconds // 86400)
    if days == 1:
        return "昨天"
    if days < 14:
        return f"{days} 天前"
    return dt.astimezone().strftime("%Y-%m-%d")


def format_local(raw: str) -> str:
    """``2026-10-04 13:25`` in local time; falls back to the raw text if it is not a timestamp."""
    dt = parse_time(raw)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else str(raw or "")


def _read_json(path: Path) -> dict:
    try:
        if not path.is_file():
            return {}
        data = json.loads(runtime.read_private(path))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def tool_report_path(root: Path, environment_id: str) -> Path:
    return root / "tool-versions" / (runtime.safe_id(environment_id) + ".json")


def diagnostics_path(root: Path, environment_id: str) -> Path:
    return root / "diagnostics" / (runtime.safe_id(environment_id) + ".json")


def load_tool_reports(root: Path, environment_id: str) -> tuple[ToolReport, ...]:
    raw = _read_json(tool_report_path(root, environment_id)).get("reports", [])
    reports = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or item.get("tool") not in TOOLS:
            continue
        reports.append(ToolReport(
            tool=item["tool"], installed=str(item.get("installed", "unknown")), latest=str(item.get("latest", "unknown")),
            status=str(item.get("status", "unknown")), source=str(item.get("source", "")), path=str(item.get("path", "")),
            channel=str(item.get("channel", "") or ""), candidate=str(item.get("candidate", "") or ""),
            publisher=str(item.get("publisher", "") or ""), message=str(item.get("message", "") or ""),
            checked_at=str(item.get("checked_at", "") or ""), can_update=bool(item.get("can_update", False))))
    reports.sort(key=lambda r: TOOLS.index(r.tool))
    return tuple(reports)


def format_tool_report(r: ToolReport) -> str:
    """Verbose per-tool diagnostics text (kept for the collapsible diagnostics block)."""
    lines = [f"{r.tool}：{r.installed} → 上游 {r.latest} · {r.source} · {r.status}", f"執行路徑：{r.path}"]
    if r.channel:
        lines.append(f"頻道：{r.channel} · 該頻道版本：{r.candidate} · 發布者：{r.publisher}")
    lines.append(f"{r.message} · 檢查時間：{r.checked_at}")
    return "\n".join(lines)


def last_used(env_id: str, records: dict[str, list[dict[str, Any]]]) -> str:
    stamps = [str(p.get("last_used") or "") for p in records.get("presets", []) if p.get("environment_id") == env_id]
    stamps += [str(s.get("updated_at") or "") for s in records.get("sessions", [])
               if s.get("environment_id") == env_id and s.get("status") != "queued"]
    stamps = [s for s in stamps if parse_time(s)]
    return max(stamps, key=lambda s: parse_time(s)) if stamps else ""


def _latest_session_failed(env_id: str, records: dict[str, list[dict[str, Any]]]) -> bool:
    sessions = [s for s in records.get("sessions", []) if s.get("environment_id") == env_id and parse_time(str(s.get("updated_at") or ""))]
    if not sessions:
        return False
    newest = max(sessions, key=lambda s: parse_time(str(s["updated_at"])))
    return newest.get("status") == "failed"


def _tool_chip(name: str, report: ToolReport | None) -> Chip:
    label = TOOL_LABEL[name]
    if report is None:
        return Chip(name, f"{label} ?", "unknown", "help", f"尚未檢查 {label} 版本")
    if report.status == "update_failed":
        return Chip(name, f"{label} 更新失敗", "danger", "x-circle", report.message or "更新未完成")
    if report.installed == "unknown":
        return Chip(name, f"{label} 未安裝", "unknown", "circle-dashed", report.message or f"此環境找不到 {label} CLI（不影響其他工具）")
    if report.status == "update_available":
        return Chip(name, f"{label} 可更新", "info", "arrow-up-circle", f"{report.installed} → {report.latest}")
    note = "比官方 registry 更新" if report.status == "newer_than_registry" else (
        "已是最新版本" if report.status == "current" else "未偵測到新版本")
    return Chip(name, f"{label} {report.installed}", "ok", "check", note)


# --------------------------------------------------------------------------- builder


def build_env_state(env: dict[str, Any], *, records: dict[str, list[dict[str, Any]]], root: Path, pinned: bool = False) -> EnvState:
    env_id = str(env.get("id", ""))
    kind = "ssh" if env.get("kind") == "ssh" else "local"
    mode = str(env.get("ssh_auth_mode", "interactive")) if kind == "ssh" else "interactive"
    remember = int(env.get("remember_seconds", 0) or 0) if kind == "ssh" else 0
    target = str(env.get("ssh_target", "") or "")
    reports = load_tool_reports(root, env_id)
    by_tool = {r.tool: r for r in reports}
    reasons: list[tuple[str, str]] = []  # (tone, text)
    chips: list[Chip] = []

    trust_ok, fingerprint, identity, proxyjump = False, "", (), ""
    agent_text, agent_checked, diag_at, ssh_version = "", False, "", ""
    openssh_missing = False
    verified = kind == "local"
    credential = "local"

    if kind == "ssh":
        try:
            from . import ssh_security
            ssh_security.executable()
        except RuntimeError:
            openssh_missing = True
        except Exception:
            openssh_missing = False
        try:
            from .askpass import security_status
            status = security_status(env, root)
        except Exception:
            status = {}
        fingerprint = str(status.get("fingerprint", "") or "")
        trust_ok = bool(fingerprint)
        diag = _read_json(diagnostics_path(root, env_id))
        identity = tuple(str(p) for p in (diag.get("identityfiles") or status.get("identityfiles") or []))
        proxyjump = str(diag.get("proxyjump", "") or "") if diag else ""
        ssh_version = str(diag.get("ssh_version", "") or "") if diag else ""
        raw_agent = str(diag.get("agent_status", "") or "")
        agent_checked = bool(diag and raw_agent)
        agent_text = AGENT_TEXT.get(raw_agent, raw_agent)
        diag_at = str(diag.get("checked_at", "") or "")
        verified = bool(diag) or bool(reports) or any(
            s.get("environment_id") == env_id and s.get("status") in {"running", "detached", "ended", "ready"}
            for s in records.get("sessions", []))

        if mode == "password" and remember != 0:
            credential = "password_persistent" if remember == -1 else "password_cached"
            label = f"Credential · {SHORT_POLICY.get(remember, str(remember) + 's')}"
            if remember == -1:
                chips.append(Chip("credential", label, "warn", "lock", "密碼不會自動到期，會一直保存在 Windows Credential Manager，直到你鎖定。這是你的設定，不是錯誤。"))
            else:
                chips.append(Chip("credential", label, "unknown", "lock", "密碼保存在 Windows Credential Manager；期限是本工具的取用政策"))
        elif mode == "password":
            credential = "password_off"
            chips.append(Chip("credential", "密碼 · 不保存", "unknown", "key", "每次連線手動輸入密碼"))
        elif mode == "key":
            credential = "key"
            chips.append(Chip("credential", "SSH key", "unknown", "key", "使用既有 SSH key／agent；passphrase 不會保存"))
        else:
            credential = "interactive"
            chips.append(Chip("credential", "互動驗證", "unknown", "key", "在終端輸入密碼／OTP；不會保存"))

        if trust_ok:
            chips.append(Chip("trust", "主機已信任", "ok", "shield-check", "已保存主機金鑰；每次連線仍會驗證"))
        else:
            chips.append(Chip("trust", "主機待核對", "unknown", "circle-dashed", "第一次連線時會要求核對主機指紋"))

        if agent_checked:
            if raw_agent == "keys available":
                chips.append(Chip("agent", "Agent · 有金鑰", "ok", "key", "ssh-agent 已載入金鑰"))
            elif raw_agent == "no keys loaded":
                tone = "warn" if mode == "key" else "unknown"
                chips.append(Chip("agent", "Agent · 無金鑰", tone, "key", "ssh-agent 目前沒有載入金鑰"))
                if mode == "key":
                    reasons.append(("warn", "SSH key 模式但 agent 沒有載入金鑰"))
            else:
                chips.append(Chip("agent", "Agent · " + agent_text, "unknown", "key", "ssh-agent 目前不可用"))
        else:
            chips.append(Chip("agent", "Agent ?", "unknown", "help", "尚未檢查；執行「環境檢查」確認（不讀取私鑰）"))

        if openssh_missing:
            reasons.append(("danger", "找不到 Windows OpenSSH Client"))
            chips.insert(0, Chip("openssh", "缺少 OpenSSH", "danger", "x-circle", "請安裝 Windows 的 OpenSSH Client 選用功能"))

    if reports:
        for name in TOOLS:
            chip = _tool_chip(name, by_tool.get(name))
            chips.append(chip)
            report = by_tool.get(name)
            if report and report.status == "update_failed":
                reasons.append(("warn", f"{TOOL_LABEL[name]} 更新失敗"))
    else:
        chips.append(Chip("cli", "CLI 未檢查", "unknown", "help", "尚未檢查此環境的 Codex／Claude 版本"))

    running = sum(1 for s in records.get("sessions", [])
                  if s.get("environment_id") == env_id and s.get("status") in RUNNING_STATUSES)
    if running:
        chips.insert(0, Chip("sessions", f"運行中 · {running}", "ok", "activity",
                             f"此環境目前有 {running} 個工作階段在運行（含背景執行）"))
    failed = kind == "ssh" and _latest_session_failed(env_id, records)
    if failed:
        reasons.append(("danger", "最近一次啟動失敗"))
        chips.insert(0, Chip("failed", "最近連線失敗", "danger", "x-circle", "最近一次使用此環境的工作階段失敗；請執行環境檢查"))

    if reasons:
        tone = max((t for t, _ in reasons), key=TONE_RANK.__getitem__)
    else:
        tone = "ok" if verified else "unknown"
    status_tip = "；".join(text for _, text in reasons) or ("沒有發現問題" if verified else "尚未驗證；執行「環境檢查」確認連線與工具")

    if kind == "local":
        subtitle = f"{platform.system()} {platform.release()} · localhost"
    else:
        subtitle = f"SSH · {target}"

    return EnvState(
        id=env_id, name=str(env.get("name") or "未命名"), kind=kind, builtin=env_id == "local", pinned=pinned, target=target,
        subtitle=subtitle, tone=tone, status_text=STATUS_TEXT[tone], status_tip=status_tip, chips=tuple(chips),
        last_used=last_used(env_id, records), auth_mode=mode, remember_seconds=remember, credential=credential,
        trust_ok=trust_ok, fingerprint=fingerprint, identity=identity, proxyjump=proxyjump, agent=agent_text,
        agent_checked=agent_checked, diag_checked_at=diag_at, ssh_version=ssh_version, openssh_missing=openssh_missing,
        tools=reports, last_failed=failed, running_sessions=running)


def matches_filter(state: EnvState, key: str) -> bool:
    if key == "local":
        return state.kind == "local"
    if key == "ssh":
        return state.kind == "ssh"
    if key == "ready":
        return state.tone == "ok"
    if key == "attention":
        return state.tone in {"warn", "danger"}
    return True


def sort_states(states: list[EnvState]) -> list[EnvState]:
    def key(s: EnvState):
        stamp = parse_time(s.last_used)
        return (not s.pinned, -(stamp.timestamp() if stamp else 0.0), s.name.casefold())
    return sorted(states, key=key)
