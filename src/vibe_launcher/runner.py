"""External terminal supervisor, status bridge and SSH transport."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import runtime
from .service import ssh_args
from .store import Store
from . import askpass

TRANSITIONS = {
    "queued": {"connecting", "waiting_auth", "running", "ended", "failed", "unknown"},
    "connecting": {"waiting_auth", "running", "detached", "ended", "failed", "unknown"},
    "waiting_auth": {"running", "ended", "failed", "unknown"},
    "running": {"detached", "ended", "failed", "unknown"},
    "detached": {"connecting", "running", "ended", "failed", "unknown"},
    "unknown": {"connecting", "waiting_auth", "running", "detached", "ended", "failed"},
    "ended": set(), "failed": set(),
}


# Exit statuses the remote supervisor uses to report how an interactive session ended (see runtime.session_outcome).
SESSION_EXITS = {runtime.EXIT_SESSION_ENDED: "ended", runtime.EXIT_SESSION_DETACHED: "detached",
                 runtime.EXIT_SESSION_FAILED: "failed"}
SESSION_EXIT_NOTES = {
    "ended": "Session ended.",
    "detached": "Session is still running in the background. Use Attach to reconnect.",
    "failed": "Session ended with an error.",
}


def process_alive(pid: int) -> bool:
    """Is the local process still running? (Never os.kill(pid, 0): on Windows that terminates the process.)"""
    if type(pid) is not int or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # access denied means it exists
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def process_started(pid: int) -> datetime | None:
    """When the running local process ``pid`` was created (UTC); None when that cannot be read (gone, not ours, not Windows)."""
    if os.name != "nt" or type(pid) is not int or pid <= 0:
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime     # 100 ns steps since 1601-01-01 UTC
        return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
    finally:
        kernel.CloseHandle(handle)


def runner_alive(pid, recorded_at: str, *, local: bool, queued: bool = False) -> bool:
    """Is ``pid`` still the runner that last wrote this session? Windows reuses PIDs, so a process that started after the
    record was written (including every process after a reboot) is someone else. A queued record is written just before
    its runner starts, so it gets a longer allowance."""
    if not process_alive(pid):
        return False
    if os.name != "nt":
        return True
    started = process_started(pid)
    if started is None:
        return not local      # a local runner is always this user's own child; one we cannot inspect is not ours
    try:
        recorded = datetime.fromisoformat(str(recorded_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=timezone.utc)
    return started <= recorded + timedelta(seconds=120 if queued else 5)


def end_local_record(root: Path, session_id: str) -> None:
    """Mark the worker's own record (``sessions/<id>.json``) of a local session that is known to be over."""
    path = runtime.session_path(root, session_id)
    try:
        if path.is_file():
            record = json.loads(runtime.read_private(path))
            if isinstance(record, dict) and record.get("status") not in {"ended", "failed"}:
                runtime.write_json(path, {**record, "status": "ended", "updated_at": runtime.now()})
    except (OSError, ValueError):
        pass


def end_stale_records(paths: list[Path], live_ids: set[str], local_environments: set[str]) -> int:
    """Worker records of local sessions that are no longer live in the database: their process is gone with the terminal."""
    ended = 0
    for path in paths:
        try:
            record = json.loads(runtime.read_private(path))
            if (isinstance(record, dict) and record.get("environment_id") in local_environments
                    and record.get("session_id") not in live_ids and record.get("status") not in {"ended", "failed"}):
                runtime.write_json(path, {**record, "status": "ended", "updated_at": runtime.now()})
                ended += 1
        except (OSError, ValueError):
            continue
    return ended


def remember_runner(store: Store, request: dict) -> None:
    """Record this terminal's pid on the session so the app can tell when the terminal window was closed."""
    session_id = request.get("session_id")
    if not session_id:
        return
    try:
        session = store.get("sessions", session_id)
        session["runner_pid"] = os.getpid()
        store.save("sessions", session)
    except Exception:
        pass


def save_tool_report(store: Store, request: dict, payload: dict) -> None:
    if not isinstance(payload, dict) or payload.get("environment_id") != request["environment_id"]:
        return
    reports = payload.get("reports")
    if not isinstance(reports, list) or not 1 <= len(reports) <= 2:
        return
    allowed = {"tool", "checked_at", "path", "installed", "latest", "source", "status", "message", "can_update", "channel", "publisher", "candidate", "previous"}
    for report in reports:
        if not isinstance(report, dict) or report.keys() - allowed or report.get("tool") not in {"codex", "claude"}:
            return
        if request["action"] == "update-tool" and report["tool"] != request["tool"]:
            return
        if any(not isinstance(v, (str, bool)) or len(str(v)) > 4096 for v in report.values()):
            return
    path = store.path.parent / "tool-versions" / (runtime.safe_id(request["environment_id"]) + ".json")
    previous = json.loads(runtime.read_private(path)).get("reports", []) if path.exists() else []
    merged = {r["tool"]: r for r in previous}
    merged.update({r["tool"]: r for r in reports})
    runtime.write_json(path, {"environment_id": request["environment_id"], "reports": list(merged.values()), "checked_at": runtime.now()})


def record_event(store: Store, request: dict, event: dict) -> None:
    if not isinstance(event, dict) or set(event) - {"updated_at", "status", "auth_status", "session_id", "pid"}:
        return
    if "status" in event and (not isinstance(event["status"], str) or event["status"] not in {"queued", "connecting", "waiting_auth", "running", "detached", "ended", "failed", "unknown", "ready"}):
        return
    if "auth_status" in event and (not isinstance(event["auth_status"], str) or event["auth_status"] not in {"authenticated", "configured", "waiting_auth", "unknown"}):
        return
    if "pid" in event and (type(event["pid"]) is not int or event["pid"] <= 0):
        return
    supplied_id = event.get("session_id")
    if supplied_id is not None:
        try:
            runtime.safe_id(supplied_id)
        except ValueError:
            return
        if request.get("action") != "list-sessions" and supplied_id != request.get("session_id") \
                and supplied_id not in request.get("session_ids", ()):
            return
    timestamp = runtime.now()
    if event.get("status") in {"unknown", "failed"} and request.get("account"):
        auth_id = request["account"]["id"] + ":" + request["environment_id"]
        try:
            auth = store.get("auth_states", auth_id)
        except KeyError:
            auth = None
        if auth and auth["status"] == "waiting_auth":
            store.save("auth_states", {"id": auth_id, "status": "unknown", "checked_at": timestamp})
    if "auth_status" in event and request.get("account"):
        current_account = store.get("accounts", request["account"]["id"])
        if current_account.get("credential_revision") != request["account"].get("credential_revision"):
            return
        store.save("auth_states", {"id": request["account"]["id"] + ":" + request["environment_id"],
                                   "status": event["auth_status"], "checked_at": timestamp})
    session_id = event.get("session_id") or request.get("session_id")
    if session_id and "status" in event:
        try:
            session = store.get("sessions", session_id)
        except KeyError:
            return
        if session["environment_id"] != request["environment_id"]:
            return
        if request.get("account") and session["account_id"] != request["account"]["id"]:
            return
        if event["status"] != session["status"] and event["status"] not in TRANSITIONS.get(session["status"], set()):
            return
        # Doctor/login readiness does not mean the coding session is running.
        if event["status"] not in {"ready"}:
            session.update(status=event["status"], updated_at=timestamp)
            if "pid" in event:
                session["pid"] = event["pid"]
            store.save("sessions", session)


def run_remote(store: Store, request: dict, environment: dict) -> int:
    # Re-read policy at launch; a queued terminal must not retain a disabled mode.
    environment = store.get("environments", environment["id"])
    if request["action"] == "doctor":
        from .ssh_security import executable, resolve
        settings = resolve(store.path.parent, environment)
        version = subprocess.run([executable(), "-V"], capture_output=True, text=True, timeout=10)
        print((version.stderr or version.stdout).strip())
        agent = subprocess.run([executable("ssh-add"), "-l"], capture_output=True, timeout=10)
        agent_status = {0: "keys available", 1: "no keys loaded", 2: "not available"}.get(agent.returncode, "unknown")
        print("SSH agent: " + agent_status)
        runtime.write_json(store.path.parent / "diagnostics" / (environment["id"] + ".json"),
                           {"ssh_version": (version.stderr or version.stdout).strip()[:250], "agent_status": agent_status,
                            "identityfiles": settings["identityfiles"], "proxyjump": settings.get("proxyjump", "none"),
                            "checked_at": runtime.now()})
        print("IdentityFile (existing SSH config; contents are not read):")
        for path in settings["identityfiles"]:
            print("  " + path)
        print("ProxyJump: " + settings.get("proxyjump", "none"))
        print("Verify first-use host fingerprints using a trusted channel. Changed keys stop the connection.")
        if request.get("local_diagnostics_only"):
            return 0
    record_event(store, request, {"status": "connecting"})
    marker = ("VIBE_EVENT_" + request["nonce"] + ":").encode("ascii")
    tool_marker = ("VIBE_TOOLS_" + request["nonce"] + ":").encode("ascii")
    quota_marker = ("VIBE_QUOTA_" + request["nonce"] + ":").encode("ascii")
    result_marker = ("VIBE_RESULT_" + request["nonce"] + ":").encode("ascii")
    operation_report = None
    quota_report = None
    auth_env = askpass.ssh_environment(environment, store.path.parent, store.path)
    pending = b""
    events = []
    tool_report = None
    authenticated = False
    cancelled = False
    process = None
    try:
        process = subprocess.Popen(ssh_args(environment["ssh_target"], request, root=store.path.parent, environment=environment), stdout=subprocess.PIPE,
                                   **({"env": auth_env} if auth_env is not None else {}))
        while chunk := process.stdout.read1(4096):
            if not authenticated:
                # stdout is the established remote channel, not SSH's local auth
                # prompts/banner (stderr). Only now may a pending password persist.
                if auth_env:
                    askpass.password_operation(auth_env, promote=True)
                authenticated = True
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if request.get("operation_id") and line.startswith(result_marker) and len(line) <= 65536:
                    try:
                        candidate = json.loads(line[len(result_marker):].strip())
                        if isinstance(candidate, dict) and set(candidate) == {"action", "operation_id", "result"} and candidate["action"] == request["action"] and candidate["operation_id"] == request["operation_id"] and isinstance(candidate["result"], dict):
                            operation_report = candidate
                    except ValueError:
                        pass
                if request["action"] in {"check-tools", "update-tool"} and line.startswith(tool_marker):
                    try:
                        tool_report = json.loads(line[len(tool_marker):].strip())
                    except ValueError:
                        pass
                if request["action"] == "quota" and line.startswith(quota_marker):
                    try:
                        quota_report = json.loads(line[len(quota_marker):].strip())
                    except ValueError:
                        pass
                # Interactive CLI output is never a control channel. Only the
                # non-PTY list helper can report state, committed after exit 0.
                if request["action"] == "list-sessions" and line.startswith(marker):
                    try:
                        event = json.loads(line[len(marker):].strip())
                        if isinstance(event, dict) and "auth_status" not in event and len(events) < 10000:
                            events.append(event)
                    except (ValueError, TypeError):
                        pass
            pending = pending[-65536:]
        code = process.wait()
        outcome = SESSION_EXITS.get(code) if request["action"] in {"launch", "attach"} else None
        if outcome:
            record_event(store, request, {"status": outcome})
            print("\n" + SESSION_EXIT_NOTES[outcome], flush=True)
            code = 1 if outcome == "failed" else 0
        if code in {0, 1} and tool_report is not None:
            save_tool_report(store, request, tool_report)
        if code == 0 and quota_report is not None and isinstance(quota_report, dict):
            runtime.write_json(runtime.quota_file(store.path.parent, request["account"]["id"], request["environment_id"]),
                               {**quota_report, "account_id": request["account"]["id"],
                                "environment_id": request["environment_id"]})
        if code == 0:
            if operation_report is not None:
                runtime.write_json(store.path.parent / "results" / (runtime.safe_id(request["operation_id"]) + ".json"), operation_report)
            if auth_env and not authenticated:
                askpass.password_operation(auth_env, promote=True)
            for event in events:
                record_event(store, request, event)
            if request["action"] == "login":
                record_event(store, request, {"auth_status": "configured" if request["account"]["auth"] == "api" else "authenticated"})
            if request["action"] in {"launch", "attach"} and not outcome:
                record_event(store, request, {"status": "unknown"})
            if request["action"] == "stop":
                for stopped in request.get("session_ids") or [request.get("session_id")]:
                    record_event(store, dict(request, session_id=stopped), {"status": "ended"})
    except KeyboardInterrupt:
        if process is not None:
            process.terminate()
            process.wait()
        code = 130
    finally:
        cancelled = askpass.was_cancelled(auth_env)
        askpass.cleanup(auth_env)
    if code:
        current = None
        if request.get("session_id"):
            current = store.get("sessions", request["session_id"])["status"]
        if current not in {"ended", "failed"}:
            # Definitely never started: the user cancelled before auth, or the remote reported a pre-start
            # failure (missing CLI, bad path...) before creating any tmux worker. Anything else may be running.
            not_started = request["action"] == "launch" and (
                (cancelled and not authenticated) or code == runtime.EXIT_NOT_STARTED)
            record_event(store, request, {"status": "failed" if not_started else "unknown"})
        if request["action"] == "list-sessions":
            for session in store.list("sessions"):
                if session["environment_id"] == request["environment_id"] and session["status"] not in {"ended", "failed"}:
                    record_event(store, request, {"session_id": session["id"], "status": "unknown"})
    return code


def _use_utf8_console() -> None:
    """Let the console decode what ssh/CLIs write as UTF-8 (the default code page here is often a legacy one)."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


def main() -> int:
    _use_utf8_console()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    job_path = Path(sys.argv[1])
    job = json.loads(runtime.read_private(job_path))
    if job_path.parent.name != "jobs" or Path(job["database"]).parent.resolve() != job_path.parent.parent.resolve():
        raise ValueError("Invalid launcher job location")
    runtime.validate_request(job["request"])
    job_path.unlink(missing_ok=True)
    store = Store(Path(job["database"]))
    request, environment = job["request"], job["environment"]
    from .i18n import catalog
    runtime._MESSAGES = catalog(request.get("language", "en"))
    remember_runner(store, request)
    print("Vibe Launcher | " + environment["name"] + " | " + request["action"], flush=True)
    print("Complete SSH verification in this terminal and account verification in the official browser.", flush=True)
    if request.get("account"):
        account = request["account"]
        print(f"Profile: {account['name']} | {account['tool']} | {account['auth']}")
    code = 1
    try:
        if environment["kind"] == "ssh":
            code = run_remote(store, request, environment)
        else:
            def event_sink(_request, event):
                record_event(store, request, event)
            runtime.emit = event_sink
            code = runtime.perform(request, store.path.parent)
    except KeyboardInterrupt:
        record_event(store, request, {"status": "unknown"})
        print("Cancelled. Remote work may still be running; refresh sessions before retrying.")
        code = 130
    except Exception as error:
        # A failed stop/attach cannot prove that the existing remote worker ended.
        status = "unknown" if environment["kind"] == "ssh" and request["action"] in {"stop", "attach"} else "failed"
        record_event(store, request, {"status": status})
        print("Operation failed: " + (str(error) if isinstance(error, (ValueError, RuntimeError))
                                     else type(error).__name__))
    report = {}
    if request.get("operation_id"):
        result_path = store.path.parent / "results" / (runtime.safe_id(request["operation_id"]) + ".json")
        if result_path.exists():
            report = json.loads(runtime.read_private(result_path, limit=65536))
        else:
            report = {"action": request["action"], "operation_id": request["operation_id"],
                      "result": {"status": "failed" if environment["kind"] == "local" else "unknown", "exit_code": code}}
        if request["action"] == "login" or (request["action"] == "github" and request.get("github_operation") == "login"):
            runtime.write_json(result_path, {**report, "runner_exit_code": code})
    print(f"\nOperation finished (exit {code}). Return to Vibe Launcher to retry or reconnect.")
    login_succeeded = False
    if code == 0 and request.get("operation_id") and (request["action"] == "login" or
            (request["action"] == "github" and request.get("github_operation") == "login")):
        try:
            login_succeeded = (report.get("action") == request["action"] and
                               report.get("operation_id") == request["operation_id"] and
                               report.get("result", {}).get("status") in {"authenticated", "configured"})
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    login_operation = request["action"] == "login" or (request["action"] == "github" and request.get("github_operation") == "login")
    # A finished background operation reports to its dialog; only a failure is worth keeping the window for.
    background_done = code == 0 and bool(request.get("operation_id")) and not login_operation
    if os.environ.get("VIBE_EMBEDDED") != "1" and not login_succeeded and not background_done:
        try:
            input("Press Enter to close this terminal...")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
