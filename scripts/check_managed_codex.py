"""End-to-end check of the launcher's managed Codex terminal, offline, on Windows.

The official native TUI (``codex --remote``) runs in the launcher's ConPTY against the launcher bridge and controller
(``runtime.ManagedCodexBridge`` / ``ManagedCodexSession``); the official ``codex app-server`` owners run in two blank,
isolated profiles and talk only to a loopback Responses fixture. Control requests travel the production file channel
(``runtime.codex_control_action``). No real account, credential, SSH host or external model is used.

    python scripts/check_managed_codex.py --output .local-test/quota-phase0/managed-02
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_codex_control import OfflineResponses  # noqa: E402
from vibe_launcher import runtime  # noqa: E402
from vibe_launcher.conpty import ConPty  # noqa: E402


class LimitedResponses(OfflineResponses):
    """The fixture, plus one usage-limit answer on demand (what the service sends when a plan's quota ran out)."""
    limit_next = False

    def do_POST(self) -> None:
        if type(self).limit_next and self.path == "/v1/responses":
            type(self).limit_next = False
            type(self).calls += 1
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.dumps({"error": {"type": "usage_limit_reached", "message": "Offline fixture usage limit",
                                         "plan_type": "plus", "resets_at": int(time.time()) + 3600}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_POST()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    executable = shutil.which("codex.exe")
    if os.name != "nt" or not executable:
        raise RuntimeError("This check requires Windows and an installed Codex CLI")
    version = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    state = root / "state"
    state.mkdir()
    project = root / "project"
    project.mkdir()
    service = ThreadingHTTPServer(("127.0.0.1", 0), LimitedResponses)
    threading.Thread(target=service.serve_forever, daemon=True).start()
    base_env = {key: value for key, value in os.environ.items() if key.upper() in {
        "SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "PATHEXT", "COMSPEC", "LOCALAPPDATA"}}
    accounts = {name: {"id": name, "name": name.upper(), "tool": "codex", "auth": "subscription", "environment_id": "local"}
                for name in ("a", "b")}
    owners: list[subprocess.Popen] = []
    errors = []

    def profile_env(account_id: str) -> dict:
        profile = runtime.profile_dir(root, accounts[account_id])
        profile.mkdir(parents=True, exist_ok=True)
        return dict(base_env, CODEX_HOME=str(profile), HOME=str(profile), USERPROFILE=str(profile))

    def factory(account_id: str, settings: dict):
        # The production factory with one difference: the official provider is replaced by the loopback fixture.
        if owners and owners[-1].poll() is None:
            raise RuntimeError("Previous writer remains alive")
        config = {"analytics.enabled": False, "model_provider": "offline_probe", "model": settings.get("model") or "offline-test",
                  "sqlite_home": str(state), "model_providers.offline_probe.name": "Offline probe",
                  "model_providers.offline_probe.base_url": f"http://127.0.0.1:{service.server_port}/v1",
                  "model_providers.offline_probe.wire_api": "responses",
                  "model_providers.offline_probe.requires_openai_auth": False,
                  "model_providers.offline_probe.supports_websockets": False,
                  "model_providers.offline_probe.request_max_retries": 0,
                  "model_providers.offline_probe.stream_max_retries": 0}
        if settings.get("reasoningEffort"):
            config["model_reasoning_effort"] = settings["reasoningEffort"]
        command = [executable, "app-server", "--stdio"]
        for key, value in config.items():
            command += ["-c", key + "=" + json.dumps(value)]
        error = (root / f"owner-{len(owners) + 1}-{account_id}.stderr.txt").open("wb")
        errors.append(error)
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error,
                                   env=profile_env(account_id), cwd=project, creationflags=0x08000000)
        owners.append(process)
        return process

    # Blank profiles are not signed in and have no native extensions: the launcher's sign-in and extension refresh are
    # replaced; everything else (fingerprints, MCP status, settings comparison, exact resume) is the production code.
    runtime.check_login = lambda *_args: True
    runtime.sync_user_data = lambda *_args, **_kwargs: {}
    runtime.prepare = lambda account, _root: profile_env(account["id"])

    request = {"session_id": "smoke", "environment_id": "local", "account": accounts["a"], "path": str(project)}
    report = {"codex_version": version, "real_accounts": False, "external_inference": False, "ssh": False}
    chunks: list[bytes] = []
    terminal = None
    bridge = runtime.ManagedCodexBridge(factory, "a", deadline=30)
    controller = runtime.ManagedCodexSession(request, root, bridge)
    bridge._validate_target = controller.validate_target
    bridge._settings_equivalent = controller.settings_equivalent
    ticking = threading.Event()
    trace: list = []
    observe = bridge._observe

    def traced(message):
        params = message.get("params") or {}
        thread = params.get("threadId") or (params.get("thread") or {}).get("id")
        item = params.get("item") or {}
        trace.append([message.get("method") or "response", thread, item.get("type"),
                      json.dumps(params)[:400] if message.get("method") in ("warning", "thread/started") else item.get("status")])
        return observe(message)
    bridge._observe = traced
    front = bridge._frontend_message

    def traced_front(message):
        params = message.get("params") or {}
        trace.append(["front:" + str(message.get("method") or "response"), params.get("threadId"),
                      sorted(params)[:12] if isinstance(params, dict) else None,
                      json.dumps(params)[:6000] if message.get("method") in ("thread/start", "turn/start", "thread/fork") else None])
        return front(message)
    bridge._frontend_message = traced_front
    rpc = bridge._rpc

    def traced_rpc(method, params):
        try:
            result = rpc(method, params)
        except Exception as error:
            trace.append(["rpc:" + method, None, "error", str(error)])
            raise
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
        trace.append(["rpc:" + method, thread.get("status"), "ok", json.dumps({k: v for k, v in result.items() if k != "thread"})[:3000]])
        return result
    bridge._rpc = traced_rpc
    equivalent = bridge._equivalent

    def traced_equivalent(source, target):
        result = equivalent(source, target)
        trace.append(["equivalent", None, result, json.dumps({"source": source, "target": target})[:6000]])
        return result
    bridge._equivalent = traced_equivalent

    def tick_forever():
        while not ticking.is_set():
            controller.tick()
            time.sleep(0.3)

    def await_state(predicate, description: str, timeout: float = 60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = bridge.snapshot()
            if predicate(snapshot):
                return snapshot
            if terminal and not terminal.is_alive():
                raise RuntimeError("Native TUI exited: " + description)
            time.sleep(0.2)
        raise TimeoutError(description)

    def submit(text: str) -> None:
        # The TUI treats a burst of keys followed at once by Enter as a paste: type, pause, then press Enter alone.
        terminal.write(text.encode())
        time.sleep(1.5)
        terminal.write(b"\r")

    def control(action: str, **values) -> dict:
        return runtime.codex_control_action(dict({"session_id": "smoke", "environment_id": "local", "control_action": action,
                                                  "accounts": list(accounts.values())}, **values), root)

    def completed(thread_id: str) -> int:
        return sum(1 for entry in list(trace) if entry[0] == "turn/completed" and entry[1] == thread_id)

    def turns() -> int:
        thread = bridge.request("thread/read", {"threadId": bridge.snapshot()["thread_id"], "includeTurns": True})
        return len(thread["thread"].get("turns") or [])

    worker = threading.Thread(target=tick_forever, daemon=True)
    try:
        endpoint, token = bridge.start()
        worker.start()
        frontend = root / "frontend"
        frontend.mkdir()
        env = dict(base_env, CODEX_HOME=str(frontend), HOME=str(frontend), USERPROFILE=str(frontend),
                   TERM="xterm-256color", VIBE_CODEX_CONTROL_TOKEN=token)
        terminal = ConPty([executable, "--remote", endpoint, "--remote-auth-token-env", "VIBE_CODEX_CONTROL_TOKEN"],
                          cwd=str(project), env=env, on_output=chunks.append)
        first = await_state(lambda s: bool(s["thread_id"]), "TUI binds a thread")
        report["native_tui_bound"] = True
        thread_id = first["thread_id"]
        time.sleep(3)
        submit("Return the offline fixture reply only.")
        await_state(lambda s: completed(thread_id) == 1 and s["can_switch"], "first turn completes")
        report["turn_1_on_a"] = bridge.snapshot()["last_turn_status"] == "completed"
        settings_before = dict(bridge._settings)
        first_owner = owners[0]

        reply = control("switch", target_account_id="b")
        report["switch_request_accepted"] = reply.get("accepted") is True
        switched = await_state(lambda s: s["account"] == "b" or s["state"] == "needs_attention", "manual handoff to b")
        report["handoff_reason"] = switched["reason"]
        report["manual_exact_handoff"] = switched["account"] == "b" and switched["thread_id"] == thread_id
        report["original_writer_exited"] = first_owner.poll() is not None and len(owners) == 2
        report["frontend_survived"] = terminal.is_alive()
        report["settings_preserved"] = bridge._settings == settings_before
        # The controller publishes at the end of the tick that performed the handoff.
        deadline = time.monotonic() + 5
        while True:
            state_file = json.loads((runtime.codex_control_path(root, "smoke") / "state.json").read_text(encoding="utf-8"))
            if state_file.get("account_id") == "b" or time.monotonic() > deadline:
                break
            time.sleep(0.1)
        report["state_reports_b"] = state_file.get("account_id") == "b" and state_file.get("thread_id") == thread_id

        submit("Second fixture turn after the handoff.")
        done = await_state(lambda s: completed(thread_id) == 2 and s["can_switch"], "turn after handoff completes")
        report["turn_2_on_b"] = done["account"] == "b" and done["last_turn_status"] == "completed"
        report["history_turns_after_handoff"] = turns()

        # Policy-driven: b is exhausted and its next turn fails on quota; a has quota. The flow must move the
        # conversation back to a at a safe point and continue the failed task there by itself.
        now = runtime.now()
        for name, used in (("a", 10), ("b", 100)):
            runtime.write_json(runtime.quota_file(root, name, "local"), {
                "tool": "codex", "account_id": name, "environment_id": "local", "checked_at": now,
                "buckets": {"codex": {"windows": [{"slot": "primary", "used_percent": used, "window_minutes": 300,
                                                   "resets_at": int(time.time()) + 3600}], "reached": None}}})
        LimitedResponses.limit_next = True
        submit("Third fixture turn that runs out of quota.")
        failed = await_state(lambda s: completed(thread_id) == 3 and s["state"] != "running", "quota failure is reported")
        report["quota_failure_kind"] = failed["last_error_kind"]
        policy = {"id": "smoke", "name": "Smoke", "environment_id": "local", "account_ids": ["a", "b"],
                  "strategy": "priority", "threshold": 10.0, "interval": 120, "when_empty": "wait", "enabled": True}
        reply = control("policy", policy=policy)
        report["policy_accepted"] = reply.get("accepted") is True
        moved = await_state(lambda s: s["account"] == "a" or s["state"] == "needs_attention", "policy moves back to a")
        report["policy_handoff_reason"] = moved["reason"]
        report["policy_selected_a"] = moved["account"] == "a" and moved["thread_id"] == thread_id
        resumed = await_state(lambda s: completed(thread_id) == 4 and s["can_switch"], "failed task continues on a", timeout=90)
        report["auto_continued_after_quota_failure"] = resumed["account"] == "a" and resumed["last_turn_status"] == "completed"
        report["continuation_sent_by_launcher"] = any(entry[0] == "front:turn/start" and entry[3] and "vibe-handoff-" in entry[3]
                                                     for entry in trace)
        report["history_turns_final"] = turns()
        report["single_writer_at_end"] = sum(owner.poll() is None for owner in owners) == 1
        report["owners_started"] = len(owners)
        thread_final = thread_id
    except Exception as error:
        report["failure"] = f"{type(error).__name__}: {error}"
        report["bridge_state"] = {key: value for key, value in bridge.snapshot().items() if key != "rate_limits"}
        thread_final = None
    finally:
        ticking.set()
        if terminal:
            terminal.close()
        worker.join(timeout=10)
        controller.close()
        report["all_owners_exited"] = all(owner.wait(timeout=30) is not None for owner in owners)
        closed = json.loads((runtime.codex_control_path(root, "smoke") / "state.json").read_text(encoding="utf-8"))
        report["state_closed"] = closed.get("status") == "closed"

    # Reopen the exact conversation in a new managed terminal, as reloading a terminal does: new bridge, `resume <id>`.
    if thread_final:
        request = dict(request, session_id="smoke-reopen", account=accounts["a"], resume_thread_id=thread_final)
        report["reopen_waits_for_no_owner"] = not runtime.thread_owner_active(root, thread_final, "smoke-reopen")
        bridge = runtime.ManagedCodexBridge(factory, "a", deadline=30)
        controller = runtime.ManagedCodexSession(request, root, bridge)
        terminal = None
        try:
            endpoint, token = bridge.start()
            env = dict(env, VIBE_CODEX_CONTROL_TOKEN=token)
            terminal = ConPty([executable, "--remote", endpoint, "--remote-auth-token-env", "VIBE_CODEX_CONTROL_TOKEN",
                               "resume", thread_final], cwd=str(project), env=env, on_output=chunks.append)
            reopened = await_state(lambda s: bool(s["thread_id"]), "reopened TUI binds the thread")
            report["reopen_exact_thread"] = reopened["thread_id"] == thread_final
            report["reopen_history_turns"] = turns()
        except Exception as error:
            report["reopen_failure"] = f"{type(error).__name__}: {error}"
        finally:
            if terminal:
                terminal.close()
            controller.close()
    for error in errors:
        error.close()
    service.shutdown()
    service.server_close()
    report["offline_fixture_requests"] = LimitedResponses.calls
    (root / "events.json").write_text(json.dumps(trace[-400:], indent=0), encoding="utf-8")
    (root / "tui-output.txt").write_bytes(b"".join(chunks))
    (root / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    required = ("native_tui_bound", "turn_1_on_a", "switch_request_accepted", "manual_exact_handoff", "original_writer_exited",
                "frontend_survived", "settings_preserved", "state_reports_b", "turn_2_on_b", "policy_accepted",
                "policy_selected_a", "auto_continued_after_quota_failure", "single_writer_at_end", "all_owners_exited",
                "state_closed", "reopen_exact_thread")
    return 0 if all(report.get(key) for key in required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
