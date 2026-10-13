"""Exercise the installed Codex against an offline Responses fixture, never real credentials.

Run with --output pointing at a new scratch directory. No SSH or external model request is made.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class OfflineResponses(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024 * 1024 or self.path != "/v1/responses":
            self.send_error(400)
            return
        self.rfile.read(length)
        type(self).calls += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        item = {"id": "msg_probe", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "Offline probe complete.", "annotations": []}]}
        response = {"id": "resp_probe", "object": "response", "status": "in_progress", "output": []}
        events = [
            {"type": "response.created", "response": response},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {**item, "status": "in_progress", "content": []}},
            {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0,
             "content_index": 0, "delta": "Offline probe complete."},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": {**response, "status": "completed", "output": [item],
             "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
        ]
        for event in events:
            self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
        self.wfile.flush()


class ProbeClient:
    def __init__(self, executable: str, profile: Path, state: Path, endpoint: str):
        profile.mkdir()
        self.events: list[dict] = []
        self.messages: queue.Queue = queue.Queue()
        env = {key: value for key, value in os.environ.items() if key.upper() in {
            "SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "PATHEXT", "COMSPEC", "LOCALAPPDATA"}}
        env.update(CODEX_HOME=str(profile), HOME=str(profile), USERPROFILE=str(profile))
        args = [executable, "app-server", "--stdio"]
        config = {"analytics.enabled": False, "model_provider": "offline_probe", "model": "offline-test",
                  "sqlite_home": str(state), "model_providers.offline_probe.name": "Offline probe",
                  "model_providers.offline_probe.base_url": endpoint,
                  "model_providers.offline_probe.wire_api": "responses",
                  "model_providers.offline_probe.requires_openai_auth": False,
                  "model_providers.offline_probe.supports_websockets": False}
        for key, value in config.items():
            args.extend(["-c", key + "=" + json.dumps(value)])
        self.errors = (profile / "stderr.txt").open("wb")
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.errors, env=env,
                                        creationflags=0x08000000 if os.name == "nt" else 0)
        self.identifier = 0
        threading.Thread(target=self._receive, daemon=True).start()

    def _receive(self) -> None:
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                pass
        self.messages.put(None)

    def send(self, message: dict) -> None:
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def next(self, deadline: float) -> dict:
        message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
        if message is None:
            raise RuntimeError("Official app-server exited; inspect isolated stderr.txt")
        if "method" in message:
            self.events.append(message)
            if "id" in message:
                self.send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported by offline probe"}})
        return message

    def call(self, method: str, params: dict) -> dict:
        self.identifier += 1
        self.send({"id": self.identifier, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            response = self.next(deadline)
            if response.get("id") == self.identifier and "method" not in response:
                if "error" in response:
                    raise RuntimeError(method + ": " + str(response["error"].get("message", "rejected")))
                return response.get("result", {})
        raise TimeoutError(method)

    def initialize(self) -> None:
        self.call("initialize", {"clientInfo": {"name": "vibe-offline-probe", "version": "1"}})
        self.send({"method": "initialized"})
        if self.call("account/read", {"refreshToken": False}).get("account") is not None:
            raise RuntimeError("Offline profile unexpectedly authenticated")

    def wait_for_turn(self, thread_id: str) -> dict:
        deadline = time.monotonic() + 30
        for event in self.events:
            if event.get("method") == "turn/completed" and event.get("params", {}).get("threadId") == thread_id:
                return event["params"]["turn"]
        while time.monotonic() < deadline:
            event = self.next(deadline)
            if event.get("method") == "turn/completed" and event.get("params", {}).get("threadId") == thread_id:
                return event["params"]["turn"]
        raise TimeoutError("turn/completed")

    def close(self) -> None:
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stdout.close()
        self.errors.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    root = options.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    executable = shutil.which("codex.exe" if os.name == "nt" else "codex")
    if not executable:
        raise RuntimeError("Codex not found on PATH")
    state = root / "state"
    state.mkdir()
    server = ThreadingHTTPServer(("127.0.0.1", 0), OfflineResponses)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    report = {"external_inference": False, "real_accounts": False, "ssh": False}
    client = None
    try:
        client = ProbeClient(executable, root / "profile-a", state, endpoint)
        client.initialize()
        report["isolated_initialization"] = True
        created = client.call("thread/start", {"model": "offline-test", "cwd": str(root),
                              "approvalPolicy": "never", "sandbox": "read-only"})
        thread_id = created["thread"]["id"]
        client.call("turn/start", {"threadId": thread_id,
                    "input": [{"type": "text", "text": "Return the offline fixture reply only. Do not execute tools."}]})
        turn = client.wait_for_turn(thread_id)
        report["structured_completion"] = turn["status"] == "completed"
        client.close()
        client = None
        client = ProbeClient(executable, root / "profile-b", state, endpoint)
        client.initialize()
        resumed = client.call("thread/resume", {"threadId": thread_id})
        report["cross_profile_exact_resume"] = resumed["thread"]["id"] == thread_id
        report["model_and_permissions_preserved"] = all(created.get(key) == resumed.get(key)
            for key in ("model", "modelProvider", "cwd", "approvalPolicy", "approvalsReviewer", "sandbox"))
        report["history_present"] = bool(resumed["thread"].get("turns"))
        report["offline_fixture_requests"] = OfflineResponses.calls
    except Exception as error:
        report["failure"] = str(error)
    finally:
        if client:
            client.close()
        server.shutdown()
        server.server_close()
    (root / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    return 1 if "failure" in report or not all(report.get(key) for key in (
        "structured_completion", "cross_profile_exact_resume", "model_and_permissions_preserved", "history_present")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
