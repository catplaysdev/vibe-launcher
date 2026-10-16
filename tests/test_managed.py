"""Managed terminals: the Codex control channel and bridge, the worker's policy, managed Claude Code.

- codex control
- managed codex session: Worker control regressions with fake quota/bridge inputs and a deterministic clock.
- managed claude: Managed Claude Code: hook events, quota from the status line, transcript handoff and the supervisor's safe point.
"""
from __future__ import annotations

import base64
import copy
import io
import json
import re
import socket
import struct
import subprocess
import sys
import time

import pytest
from vibe_launcher import runtime, selfcmd
from vibe_launcher.codex_control import CodexControlError, ManagedCodexBridge


# --- codex control ----------------------------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def english(restore_controls):
    """These tests read English messages (the suite otherwise runs with the Traditional Chinese runtime catalog)."""
    from vibe_launcher import i18n, runtime as engine
    i18n.set_language("en")
    engine._MESSAGES = {}


@pytest.fixture
def managed(tmp_path):
    script = tmp_path / "backend.py"
    script.write_text('''import json, sys
thread = {"id": "thread-one", "status": {"type": "idle"}, "turns": [{"id": "turn-one", "status": "completed"}]}
settings = {"model": "fixture", "modelProvider": "fixture", "reasoningEffort": "medium", "cwd": "fixture", "approvalPolicy": "never", "sandbox": {"type": "readOnly"}, "instructionSources": [], "disabledPluginIds": []}
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request: continue
    result = {}
    if method == "thread/read": result = {"thread": thread}
    if method == "plugin/list": result = {"data": "x" * (13 * 1024 * 1024)}
    if method == "thread/resume":
        with open(sys.argv[0] + "." + sys.argv[1] + ".resume", "a", encoding="utf-8") as log:
            log.write(json.dumps(request.get("params")) + chr(10))
    if method in ("thread/start", "thread/resume"):
        result = dict(settings, thread=thread)
        if sys.argv[1] == "wrong": result["thread"] = dict(thread, id="wrong-thread")
        if sys.argv[1] == "different": result["model"] = "different-model"
        if sys.argv[1] == "paths": result["instructionSources"] = ["/target-profile/AGENTS.md"]
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
''', encoding="utf-8")
    processes = []
    def factory(account, settings):
        # Assert old writer has exited before the next owner can start.
        assert not processes or processes[-1].poll() is not None
        process = subprocess.Popen([sys.executable, "-u", str(script), account],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        processes.append(process)
        return process
    bridge = ManagedCodexBridge(factory, "source", deadline=2)
    endpoint, capability = bridge.start()
    yield bridge, endpoint, capability, processes
    bridge.close()


def _connect(endpoint, capability):
    port = int(endpoint.split(":")[-1].rstrip("/"))
    conn = socket.create_connection(("127.0.0.1", port), timeout=3)
    key = base64.b64encode(b"0123456789abcdef").decode()
    conn.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer {capability}\r\n\r\n").encode())
    header = bytearray()
    while not header.endswith(b"\r\n\r\n"):
        header.extend(conn.recv(1))
    return conn, bytes(header)


def _send(conn, message, *, opcode=1, final=True):
    payload = json.dumps(message).encode() if isinstance(message, dict) else message
    mask = b"test"
    length = len(payload)
    prefix = bytes(((0x80 if final else 0) | opcode, 0x80 | length)) if length < 126 else (
        bytes(((0x80 if final else 0) | opcode, 0xFE)) + struct.pack("!H", length))
    conn.sendall(prefix + mask + bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)))


def _receive(conn):
    first, size = ManagedCodexBridge._exact(conn, 2)
    if size == 126:
        size = struct.unpack("!H", ManagedCodexBridge._exact(conn, 2))[0]
    elif size == 127:
        size = struct.unpack("!Q", ManagedCodexBridge._exact(conn, 8))[0]
    payload = ManagedCodexBridge._exact(conn, size)
    return first & 15, json.loads(payload) if first & 15 == 1 else payload


def _initialize(conn):
    _send(conn, {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "test", "version": "1"}}})
    assert _receive(conn)[1]["id"] == 1
    _send(conn, {"method": "initialized"})
    _send(conn, {"id": 2, "method": "thread/start", "params": {}})
    assert _receive(conn)[1]["result"]["thread"]["id"] == "thread-one"


def test_exact_handoff_retains_frontend_and_one_owner(managed):
    bridge, endpoint, capability, processes = managed
    conn, header = _connect(endpoint, capability)
    assert b"101" in header
    _initialize(conn)
    bridge.request_switch("target")
    assert bridge.tick()["account"] == "target"
    assert processes[0].poll() is not None and processes[1].poll() is None
    _send(conn, {"id": 3, "method": "account/read", "params": {}})
    assert _receive(conn)[1]["id"] == 3
    assert bridge.snapshot()["thread_id"] == "thread-one"
    with pytest.raises(CodexControlError):
        bridge.resume_work()
    conn.close()


@pytest.mark.parametrize("target", ["wrong", "different"])
def test_failed_target_never_executes_or_claims_success(managed, target):
    bridge, endpoint, capability, processes = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    bridge.request_switch(target)
    result = bridge.tick()
    # The failed target is stopped and the same thread is resumed on the source account: one writer, reported truthfully.
    assert result["state"] == "idle" and result["account"] == "source" and result["pending_account"] is None
    assert "original account" in result["reason"]
    assert [process.args[-1] for process in processes] == ["source", target, "source"]
    assert all(process.poll() is not None for process in processes[:2]) and processes[2].poll() is None
    assert result["backend_pid"] == processes[2].pid
    conn.close()


def test_a_source_that_cannot_be_restored_leaves_nothing_running(managed):
    bridge, endpoint, capability, processes = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    factory = bridge._factory
    bridge._factory = lambda account, settings: factory("wrong" if len(processes) >= 2 else account, settings)
    bridge.request_switch("different")
    result = bridge.tick()
    assert result["state"] == "needs_attention" and result["account"] == "source"
    assert all(process.poll() is not None for process in processes)
    conn.close()


def test_bad_auth_rejected_and_picker_is_read_only(managed):
    _bridge, endpoint, capability, _ = managed
    bad, header = _connect(endpoint, "wrong-secret")
    assert b"403" in header
    bad.close()
    conn, header = _connect(endpoint, capability)
    assert b"101" in header
    _initialize(conn)
    second, header = _connect(endpoint, capability)
    assert b"101" in header
    _send(second, {"id": 1, "method": "initialize", "params": {}})
    assert "result" in _receive(second)[1]
    _send(second, {"method": "initialized"})
    _send(second, {"id": 2, "method": "thread/list", "params": {}})
    _send(conn, {"id": 3, "method": "account/read", "params": {}})
    assert _receive(second)[1] == {"id": 2, "result": {}}
    assert _receive(conn)[1]["id"] == 3
    _send(second, {"id": 3, "method": "turn/start", "params": {"threadId": "other"}})
    assert "error" in _receive(second)[1]
    second.close()
    _send(conn, {"id": 4, "method": "account/read", "params": {}})
    assert _receive(conn)[1]["id"] == 4
    assert _bridge.snapshot()["thread_id"] == "thread-one"
    conn.close()


def test_resume_rebinds_after_success_and_refuses_an_active_turn(managed):
    bridge, endpoint, capability, _ = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    with bridge._lock:
        bridge._thread_id = "old-thread"
        bridge._settings_unknown = True  # a native permission profile blocks handoff, not user-selected history
        bridge._last_error_kind = "quota"
        bridge._continuation_ready = True
    _send(conn, {"id": 3, "method": "thread/resume", "params": {"threadId": "thread-one"}})
    assert _receive(conn)[1]["result"]["thread"]["id"] == "thread-one"
    assert bridge.snapshot()["thread_id"] == "thread-one"
    assert not bridge.snapshot()["continuation_ready"]
    bridge._observe({"method": "turn/started", "params": {"threadId": "thread-one", "turn": {"id": "active"}}})
    _send(conn, {"id": 4, "method": "thread/resume", "params": {"threadId": "other"}})
    assert "error" in _receive(conn)[1]
    assert bridge.snapshot()["thread_id"] == "thread-one"
    conn.close()


def test_fragments_ping_and_login_block(managed):
    _bridge, endpoint, capability, _ = managed
    conn, _ = _connect(endpoint, capability)
    payload = json.dumps({"id": 1, "method": "account/login/start", "params": {}}).encode()
    _send(conn, payload[:20], final=False)
    _send(conn, b"alive", opcode=9)
    assert _receive(conn) == (10, b"alive")
    _send(conn, payload[20:], opcode=0)
    assert _receive(conn)[1]["error"]["message"] == runtime.tr("Use the Launcher Accounts page to sign in or sign out")
    conn.close()


def test_running_tools_approvals_and_unknown_children_prevent_handoff(managed):
    bridge, endpoint, capability, processes = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    bridge._observe({"method": "turn/started", "params": {"threadId": "thread-one", "turn": {"id": "turn-current"}}})
    bridge.request_switch("target")
    assert bridge.tick()["state"] == "running"
    bridge._observe({"method": "item/started", "params": {"threadId": "thread-one", "item": {"id": "tool", "type": "commandExecution"}}})
    bridge._observe({"method": "turn/completed", "params": {"threadId": "thread-one", "turn": {"id": "turn-current", "status": "completed"}}})
    assert not bridge.tick()["can_switch"]
    bridge._observe({"method": "item/completed", "params": {"threadId": "thread-one", "item": {"id": "tool", "type": "commandExecution", "status": "completed", "exitCode": 0}}})
    bridge._observe({"id": 100, "method": "item/commandExecution/requestApproval", "params": {"threadId": "thread-one"}})
    assert not bridge.tick()["can_switch"]
    bridge._approvals.clear()
    bridge._observe({"method": "turn/started", "params": {"threadId": "child"}})
    assert bridge.tick()["state"] == "needs_attention"
    assert len(processes) == 1
    conn.close()


def test_unknown_status_and_snapshot_never_include_sensitive_payload(managed):
    bridge, endpoint, capability, _ = managed
    bridge.request_switch("target")
    assert bridge.tick()["state"] == "unknown"
    serialized = json.dumps(bridge.snapshot())
    assert capability not in serialized and endpoint not in serialized


def test_duplicate_id_and_timeout_fail_closed(managed):
    bridge, _endpoint, _capability, _ = managed
    bridge._pending[1] = ("initialize", {}, time.monotonic())
    with pytest.raises(CodexControlError, match="Duplicate"):
        bridge._frontend_message({"id": 1, "method": "initialize", "params": {}})
    bridge._pending[1] = ("initialize", {}, time.monotonic() - 10)
    assert bridge.tick()["state"] == "needs_attention"


def test_quota_continuation_only_once_after_verified_handoff(managed):
    bridge, endpoint, capability, _ = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    bridge._observe({"method": "turn/started", "params": {"threadId": "thread-one", "turn": {"id": "turn-quota"}}})
    bridge._observe({"method": "turn/completed", "params": {"threadId": "thread-one", "turn": {
        "id": "turn-quota", "status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded"}}}})
    bridge.request_switch("target")
    assert bridge.tick()["account"] == "target"
    assert bridge.resume_work()["state"] == "running"
    with pytest.raises(CodexControlError):
        bridge.resume_work()
    conn.close()


def test_preflight_failure_retains_source_and_frontend_input(managed):
    bridge, endpoint, capability, processes = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    def reject(_account, _settings):
        raise CodexControlError("Unsupported target capability")
    bridge._validate_target = reject
    bridge.request_switch("target")
    assert bridge.tick()["state"] == "needs_attention"
    assert len(processes) == 1 and processes[0].poll() is None
    _send(conn, {"id": 3, "method": "turn/start", "params": {"threadId": "thread-one", "input": []}})
    assert _receive(conn)[1]["id"] == 3
    assert bridge.snapshot()["state"] == "running"
    assert bridge.cancel_switch()["pending_account"] is None
    conn.close()


def _live_settings(**overrides):
    # Version-matched ThreadSettings, not ThreadResumeResponse's similarly named fields.
    return {"approvalPolicy": "never", "approvalsReviewer": "user",
            "collaborationMode": {"mode": "default", "settings": {
                "model": "fixture", "reasoning_effort": "medium", "developer_instructions": None}},
            "cwd": "fixture", "model": "fixture", "modelProvider": "fixture",
            "sandboxPolicy": {"type": "readOnly"}, "effort": "medium",
            "disabledPluginIds": [], "personality": None, "summary": None,
            "activePermissionProfile": None, **overrides}


def test_real_settings_notification_updates_live_snapshot_before_switch(managed):
    bridge, endpoint, capability, processes = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    bridge._observe({"method": "thread/settings/updated", "params": {
        "threadId": "thread-one", "threadSettings": _live_settings(model="live-model", effort="high")}})
    assert bridge._settings["model"] == "live-model"
    assert bridge._settings["reasoningEffort"] == "high"
    assert bridge._settings["sandbox"] == {"type": "readOnly"}
    bridge.request_switch("target")
    # Fake authoritative resume returns fixture/medium; mismatch must preserve source.
    assert bridge.tick()["state"] == "needs_attention"
    assert len(processes) == 1 and processes[0].poll() is None
    conn.close()


def test_unknown_live_settings_require_manual_verification(managed):
    bridge, endpoint, capability, processes = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    bridge._observe({"method": "thread/settings/updated", "params": {
        "threadId": "thread-one", "threadSettings": _live_settings(summary="detailed")}})
    bridge.request_switch("target")
    assert not bridge.tick()["can_switch"]
    assert len(processes) == 1
    conn.close()


@pytest.mark.parametrize("prefix", [b"\x81\x7f", b"\x81\xff" + struct.pack("!Q", runtime._BRIDGE_LIMIT + 1)])
def test_unmasked_or_oversized_frame_never_reaches_backend(managed, prefix):
    bridge, endpoint, capability, processes = managed
    conn, _ = _connect(endpoint, capability)
    conn.sendall(prefix)
    assert conn.recv(1) == b""
    assert bridge.snapshot()["state"] == "needs_attention"
    assert not bridge._pending
    assert len(processes) == 1
    conn.close()


def test_second_text_frame_during_fragment_is_not_replayed(managed):
    bridge, endpoint, capability, _ = managed
    conn, _ = _connect(endpoint, capability)
    _send(conn, b'{"id":1,', final=False)
    _send(conn, {"id": 2, "method": "initialize", "params": {}})
    assert conn.recv(1) == b""
    assert not bridge._pending and bridge._init is None
    conn.close()


def test_forced_owner_termination_fails_handoff_gate():
    class Pipe:
        def close(self):
            pass
    class StubbornOwner:
        stdin = Pipe()
        stdout = Pipe()
        killed = False
        def poll(self):
            return 1 if self.killed else None
        def wait(self, timeout):
            if not self.killed:
                raise subprocess.TimeoutExpired("fixture-owner", timeout)
            return 1
        def kill(self):
            self.killed = True
    owner = StubbornOwner()
    bridge = ManagedCodexBridge(lambda *_args: owner, "source", deadline=0.01)
    bridge._process = owner
    with pytest.raises(CodexControlError, match=re.escape(runtime.tr("Previous owner required forced termination; outcome unknown"))):
        bridge._stop_backend()
    assert owner.killed and bridge._process is None


def test_equivalence_callback_cannot_relax_model_or_permissions():
    bridge = ManagedCodexBridge(lambda *_args: None, "source", settings_equivalent=lambda *_args: True)
    source = {"model": "fixture", "approvalPolicy": "never", "instructionSources": ["profile-a"]}
    assert bridge._equivalent(source, {**source, "instructionSources": ["profile-b"]})
    assert not bridge._equivalent(source, {**source, "model": "different"})
    assert not bridge._equivalent(source, {**source, "approvalPolicy": "untrusted"})


@pytest.mark.parametrize("item", [
    {"id": "tool-pending", "type": "commandExecution", "status": "inProgress", "exitCode": None, "processId": "session"},
    {"id": "tool-pending", "type": "commandExecution", "status": "completed", "exitCode": None},
    {"id": "tool-pending", "type": "fileChange", "status": "inProgress"},
    {"id": "tool-pending", "type": "mcpToolCall", "status": "inProgress"},
])
def test_completed_event_with_unfinished_tool_outcome_blocks_handoff(managed, item):
    bridge, endpoint, capability, processes = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    bridge._observe({"method": "turn/started", "params": {
        "threadId": "thread-one", "turn": {"id": "turn-tool"}}})
    bridge._observe({"method": "item/started", "params": {"threadId": "thread-one", "item": item}})
    bridge._observe({"method": "item/completed", "params": {"threadId": "thread-one", "item": item}})
    bridge._observe({"method": "turn/completed", "params": {"threadId": "thread-one", "turn": {
        "id": "turn-tool", "status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded"}}}})
    bridge.request_switch("target")
    result = bridge.tick()
    assert not result["can_switch"] and not result["tool_outcomes_known"]
    assert result["active_tools"] == 1
    assert len(processes) == 1 and processes[0].poll() is None
    with pytest.raises(CodexControlError):
        bridge.resume_work()
    conn.close()


def test_old_turn_completion_cannot_release_new_turn_same_thread(managed):
    bridge, endpoint, capability, processes = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    bridge._observe({"method": "turn/started", "params": {
        "threadId": "thread-one", "turn": {"id": "turn-old"}}})
    bridge._observe({"method": "turn/started", "params": {
        "threadId": "thread-one", "turn": {"id": "turn-new"}}})
    bridge._observe({"method": "turn/completed", "params": {"threadId": "thread-one", "turn": {
        "id": "turn-old", "status": "completed"}}})
    bridge.request_switch("target")
    result = bridge.tick()
    assert result["state"] != "idle" and not result["can_switch"]
    assert len(processes) == 1
    conn.close()


def test_unbound_thread_completion_never_claims_verified_idle(managed):
    bridge, _endpoint, _capability, processes = managed
    bridge._observe({"method": "turn/completed", "params": {"threadId": "unowned-thread", "turn": {
        "id": "unowned-turn", "status": "completed"}}})
    assert bridge.snapshot()["state"] != "idle"
    bridge.request_switch("target")
    assert not bridge.tick()["can_switch"] and len(processes) == 1


def test_matching_turn_and_final_tool_outcome_release_only_the_current_turn(managed):
    bridge, endpoint, capability, _ = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    bridge._observe({"method": "turn/started", "params": {
        "threadId": "thread-one", "turn": {"id": "current"}}})
    item = {"id": "command", "type": "commandExecution", "status": "inProgress"}
    bridge._observe({"method": "item/completed", "params": {"threadId": "thread-one", "item": item}})
    assert bridge.snapshot()["active_tools"] == 1
    bridge._observe({"method": "item/completed", "params": {"threadId": "thread-one",
        "item": {**item, "status": "completed", "exitCode": 0}}})
    bridge._observe({"method": "turn/completed", "params": {"threadId": "thread-one",
        "turn": {"id": "current", "status": "completed"}}})
    assert bridge.snapshot()["can_switch"]
    conn.close()


def test_missing_turn_id_cannot_release_bound_thread(managed):
    bridge, endpoint, capability, _ = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    bridge._observe({"method": "turn/completed", "params": {
        "threadId": "thread-one", "turn": {"status": "completed"}}})
    assert not bridge.snapshot()["can_switch"]
    conn.close()


def test_exact_uses_one_deadline_across_partial_reads(monkeypatch):
    from vibe_launcher import runtime
    clock = [0.0]
    class SlowSocket:
        timeouts = []
        def settimeout(self, timeout):
            self.timeouts.append(timeout)
        def recv(self, _size):
            clock[0] += 0.4
            return b"x"
    conn = SlowSocket()
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    with pytest.raises(TimeoutError, match="deadline"):
        ManagedCodexBridge._exact(conn, 20, 1.0)
    assert conn.timeouts == pytest.approx([1.0, 0.6, 0.2])


@pytest.mark.parametrize("phase", ["handshake", "frame", "fragment"])
def test_partial_websocket_message_expires_as_a_whole(managed, phase):
    bridge, endpoint, capability, _ = managed
    bridge._deadline = 0.2
    if phase == "handshake":
        port = int(endpoint.split(":")[-1].rstrip("/"))
        conn = socket.create_connection(("127.0.0.1", port), timeout=2)
        conn.sendall(b"G")
    else:
        conn, _ = _connect(endpoint, capability)
        if phase == "frame":
            conn.sendall(b"\x81")
        else:
            _send(conn, b'{"id":', final=False)
    started = time.monotonic()
    assert conn.recv(1) == b""
    assert time.monotonic() - started < 1.5
    assert not bridge._pending
    conn.close()


def test_unauthenticated_clients_are_bounded_and_slots_recover(managed):
    bridge, endpoint, capability, _ = managed
    bridge._deadline = 0.4
    port = int(endpoint.split(":")[-1].rstrip("/"))
    clients = [socket.create_connection(("127.0.0.1", port), timeout=2) for _ in range(4)]
    try:
        rejected = socket.create_connection(("127.0.0.1", port), timeout=2)
        assert rejected.recv(1) == b""
        rejected.close()
        for conn in clients:
            assert conn.recv(1) == b""
        bridge._deadline = 2
        conn, header = _connect(endpoint, capability)
        assert b"101" in header
        conn.close()
    finally:
        for conn in clients:
            conn.close()


def test_ping_does_not_renew_fragment_deadline(managed):
    bridge, endpoint, capability, _ = managed
    bridge._deadline = 0.4
    conn, _ = _connect(endpoint, capability)
    _send(conn, b'{"id":', final=False)
    started = time.monotonic()
    time.sleep(0.2)
    _send(conn, b"ping", opcode=9)
    assert _receive(conn) == (10, b"ping")
    assert conn.recv(1) == b""
    assert time.monotonic() - started < 0.58
    assert not bridge._pending
    conn.close()


def test_permission_profile_cannot_be_requested_on_the_target_and_blocks_switch(managed):
    bridge, endpoint, capability, processes = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    bridge._observe({"method": "thread/settings/updated", "params": {
        "threadId": "thread-one", "threadSettings": _live_settings(activePermissionProfile={"id": "custom"})}})
    bridge.request_switch("target")
    assert not bridge.tick()["can_switch"] and len(processes) == 1
    conn.close()


@pytest.mark.parametrize("key,value,reported", [("personality", "friendly", "friendly"), ("summary", "detailed", "detailed"),
                                               ("personality", "friendly", None)])
def test_personality_and_summary_are_requested_on_the_target_and_checked(managed, key, value, reported):
    bridge, endpoint, capability, processes = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    bridge._observe({"method": "thread/settings/updated", "params": {
        "threadId": "thread-one", "threadSettings": {key: value}}})
    assert bridge._extra[key] == value
    bridge.request_switch("target")
    switched = bridge.tick()
    assert switched["account"] == "target" and len(processes) == 2
    with open(processes[0].args[2] + ".target.resume", encoding="utf-8") as log:
        resume = json.loads(log.read().splitlines()[-1])
    if key == "personality":
        assert resume["personality"] == value
    else:
        assert resume["config"]["model_reasoning_summary"] == value
    assert not switched["can_switch"]                   # until the new owner reports its settings
    bridge._observe({"method": "thread/settings/updated", "params": {
        "threadId": "thread-one", "threadSettings": {key: reported}}})
    snapshot = bridge.snapshot()
    if reported == value:
        assert snapshot["can_switch"] and snapshot["state"] == "idle"
    else:
        assert snapshot["state"] == "needs_attention" and not snapshot["can_switch"]
    conn.close()


def test_large_native_answer_reaches_the_terminal_in_fragments(managed):
    """A real plugin/list answer measured 13 MB on one line; it must pass through, split below the client's frame limit."""
    bridge, endpoint, capability, _ = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    _send(conn, {"id": 9, "method": "plugin/list", "params": {}})
    conn.settimeout(30)
    payload, frames, final = bytearray(), 0, False
    while not final:
        first, size = ManagedCodexBridge._exact(conn, 2)
        size = struct.unpack("!Q", ManagedCodexBridge._exact(conn, 8))[0] if size == 127 else (
            struct.unpack("!H", ManagedCodexBridge._exact(conn, 2))[0] if size == 126 else size)
        assert first & 15 == (1 if not frames else 0) and size <= 4 * 1024 * 1024
        payload.extend(ManagedCodexBridge._exact(conn, size))
        frames, final = frames + 1, bool(first & 0x80)
    message = json.loads(payload)
    assert message["id"] == 9 and len(message["result"]["data"]) == 13 * 1024 * 1024 and frames == 4
    assert bridge.snapshot()["state"] == "idle"
    conn.close()


@pytest.mark.parametrize("servers, accepted", [
    ({}, True),
    ({"docs": {"enabled": False}, "memory": {"enabled": False}}, True),     # as sent with MCP servers configured
    ({"docs": {"enabled": False}, "memory": {"enabled": True}}, False),
    ({"docs": {}}, False),
    (None, False),
])
def test_title_side_thread_is_recognised_only_when_tool_free(servers, accepted):
    config = {"features." + name: False for name in ("shell_tool", "unified_exec", "multi_agent", "apps", "plugins")}
    if servers is not None:
        config["mcp_servers"] = servers
    params = {"ephemeral": True, "sandbox": "read-only", "dynamicTools": [], "threadSource": "thread_title", "config": config}
    assert ManagedCodexBridge._tool_free_side_thread(params) is accepted
    assert not ManagedCodexBridge._tool_free_side_thread(dict(params, sandbox="workspace-write"))
    assert not ManagedCodexBridge._tool_free_side_thread(dict(params, dynamicTools=[{"name": "tool"}]))


def test_account_differences_leave_the_source_running_and_report_what_is_missing(managed):
    bridge, endpoint, capability, processes = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)

    def lacking(_account, _settings):
        raise runtime.CodexHandoffBlocked("target lacks pages", [{"kind": "plugin", "name": "pages"}])
    bridge._validate_target = lacking
    bridge.request_switch("target")
    snapshot = bridge.tick()
    assert snapshot["state"] == "idle" and snapshot["account"] == "source" and snapshot["pending_account"] is None
    assert {k: v for k, v in snapshot["blocked"].items() if k != "at"} == {"target": "target", "gaps": [{"kind": "plugin", "name": "pages"}]}
    assert len(processes) == 1 and processes[0].poll() is None and snapshot["can_switch"]
    bridge._validate_target = None
    bridge.request_switch("target")
    assert bridge.snapshot()["blocked"] is None
    assert bridge.tick()["account"] == "target"
    conn.close()


def test_carried_settings_are_confirmed_by_a_turn_without_a_contrary_report(managed):
    bridge, endpoint, capability, processes = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    bridge._observe({"method": "thread/settings/updated", "params": {"threadId": "thread-one",
                                                                      "threadSettings": {"personality": "friendly"}}})
    bridge.request_switch("target")
    assert bridge.tick()["account"] == "target" and not bridge.snapshot()["can_switch"]
    bridge._observe({"method": "turn/started", "params": {"threadId": "thread-one", "turn": {"id": "turn-two"}}})
    bridge._observe({"method": "turn/completed", "params": {"threadId": "thread-one",
                                                             "turn": {"id": "turn-two", "status": "completed"}}})
    assert bridge.snapshot()["can_switch"]
    conn.close()


def test_a_second_switch_compares_with_the_new_owners_own_settings(managed):
    """Instruction files sit at each profile's own path; after a verified handoff the target's view is the baseline."""
    bridge, endpoint, capability, processes = managed
    conn, _ = _connect(endpoint, capability)
    _initialize(conn)
    bridge._settings_equivalent = lambda source, target: True       # the controller verified the files' content
    bridge.request_switch("paths")
    assert bridge.tick()["account"] == "paths"
    assert bridge._settings["instructionSources"] == ["/target-profile/AGENTS.md"]
    bridge._settings_equivalent = None
    bridge.request_switch("paths-again")
    bridge._settings_equivalent = lambda source, target: True
    assert bridge.tick()["account"] == "paths-again"
    conn.close()


# --- managed codex session --------------------------------------------------------------------------------------------
ACCOUNTS = [{"id": key, "name": key, "tool": "codex", "auth": "subscription", "environment_id": "local"}
            for key in ("a", "b")]


def policy(**overrides):
    return runtime.QuotaPolicy.from_dict(dict(dict(id="flow", name="Flow", environment_id="local",
        account_ids=["a", "b"], interval=60), **overrides))


def quota(identifier, used, checked=1000):
    return {"tool": "codex", "account_id": identifier, "environment_id": "local", "checked_at": checked,
            "buckets": {"codex": {"windows": [{"used_percent": used, "resets_at": 1300}]}}}


class FakeMonitor:
    def __init__(self, *_args):
        self.values = {}
        self.configured = None
    def configure(self, policy, accounts):
        self.configured = policy
        self.due = None
    def require_after(self, accounts, moment):
        self.due = (set(accounts), moment)
    def snapshots(self):
        return copy.deepcopy(self.values)
    def close(self):
        pass


class FakeBridge:
    def __init__(self):
        self.data = dict(state="idle", reason="", account="a", thread_id="thread-one", can_switch=True,
            pending_account=None, last_error_kind=None, rate_limits=None, rate_limits_checked_at=None)
        self.switched = []
        self.resumed = 0
    def snapshot(self):
        return copy.deepcopy(self.data)
    def request_switch(self, target):
        self.data["pending_account"] = target
    def cancel_switch(self):
        self.data["pending_account"] = None
    def tick(self):
        if self.data["pending_account"] and self.data["can_switch"]:
            self.data["account"] = self.data["pending_account"]
            self.switched.append(self.data["account"])
            self.data["pending_account"] = None
        return self.snapshot()
    def simulate_quota_failure(self):
        if self.data["state"] != "idle":
            return False
        self.data.update(last_error_kind="usageLimitExceeded", last_error_at=runtime.time.time())
        return True
    def resume_work(self, recovered=False):
        self.resumed += 1
        self.recovered = recovered
        self.data["state"] = "running"
        self.data["can_switch"] = False
        return self.snapshot()
    def close(self):
        self.data["state"] = "closed"


@pytest.fixture
def session(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    monkeypatch.setattr(runtime, "CodexQuotaMonitor", FakeMonitor)
    bridge = FakeBridge()
    controller = runtime.ManagedCodexSession({"session_id": "session", "environment_id": "local",
        "account": ACCOUNTS[0], "path": str(tmp_path)}, tmp_path, bridge)
    yield controller, bridge, clock
    controller._closed.set()


def command(action, **values):
    return {"session_id": "session", "environment_id": "local", "control_action": action, **values}


def test_policy_switch_waits_for_bridge_checkpoint_and_commits_after_success(session):
    controller, bridge, _ = session
    controller.command(command("policy", policy=policy(strategy="round-robin").to_dict(), accounts=ACCOUNTS))
    controller.monitor.values = {"a": quota("a", 100), "b": quota("b", 20)}
    bridge.data.update(state="running", can_switch=False)
    controller.tick()
    assert bridge.data["pending_account"] == "b" and controller.cursor is None
    bridge.data.update(state="idle", can_switch=True)
    controller.tick()
    assert bridge.switched == ["b"] and controller.cursor == "b"


def test_cancel_removes_pending_switch_and_monitoring(session):
    controller, bridge, _ = session
    controller.command(command("policy", policy=policy().to_dict(), accounts=ACCOUNTS))
    bridge.request_switch("b")
    controller.waiting = True
    controller.command(command("cancel"))
    controller.tick()
    assert not bridge.switched and bridge.data["pending_account"] is None
    assert controller.policy is None and controller.monitor.configured is None
    assert not controller.waiting


@pytest.mark.parametrize("action", ["switch", "policy"])
def test_other_terminal_command_cannot_mutate_pool(session, action):
    controller, _, _ = session
    with pytest.raises(ValueError):
        controller.command(command(action, session_id="another", accounts=ACCOUNTS,
            target_account_id="b", policy=policy().to_dict()))
    assert set(controller.accounts) == {"a"}


def test_control_rejects_credential_metadata_before_persistence(session):
    controller, _, _ = session
    with pytest.raises(ValueError):
        controller.command(command("switch", target_account_id="b", accounts=[dict(ACCOUNTS[1], token="fixture-secret")]))
    assert "fixture-secret" not in json.dumps(controller.status())
    assert not any("fixture-secret" in path.read_text(encoding="utf-8")
                   for path in controller.folder.rglob("*") if path.is_file())


def test_manual_handoff_resumes_only_quota_failure(session):
    controller, bridge, _ = session
    bridge.data["last_error_kind"] = "usageLimitExceeded"
    controller.command(command("switch", target_account_id="b", accounts=ACCOUNTS))
    controller.tick()
    controller.tick()
    assert bridge.switched == ["b"] and bridge.resumed == 1


def test_notify_only_policy_does_not_autoresume(session):
    controller, bridge, _ = session
    controller.command(command("policy", policy=policy(when_empty="notify-only").to_dict(), accounts=ACCOUNTS))
    controller.monitor.values = {"a": quota("a", 100), "b": quota("b", 20)}
    bridge.data["last_error_kind"] = "usageLimitExceeded"
    controller.tick()
    assert bridge.resumed == 0


def test_recovery_of_current_account_leaves_wait_state(session):
    controller, _, _ = session
    controller.command(command("policy", policy=policy().to_dict(), accounts=ACCOUNTS))
    controller.monitor.values = {"a": quota("a", 100)}
    assert controller.tick()["status"] == "waiting_quota"
    controller.monitor.values = {"a": quota("a", 20)}
    assert controller.tick()["status"] == "idle"
    assert controller.next_retry is None


def test_stale_structured_event_cannot_override_fresh_exhausted_cache(session):
    controller, bridge, clock = session
    controller.command(command("policy", policy=policy().to_dict(), accounts=ACCOUNTS))
    clock[0] = 1500
    controller.monitor.values = {"a": quota("a", 100, 1500), "b": quota("b", 20, 1500)}
    bridge.data.update(rate_limits={"limitId": "codex", "primary": {"usedPercent": 0,
        "windowDurationMins": 300, "resetsAt": 2000}}, rate_limits_checked_at=1000)
    controller.tick()
    assert bridge.switched == ["b"]


@pytest.mark.parametrize("payload", [None, [], "bad"])
def test_malformed_control_file_does_not_kill_worker(session, payload):
    controller, bridge, _ = session
    path = controller.folder / "commands" / "invalid.json"
    runtime.write_json(path, payload)
    controller.tick()
    assert not path.exists() and not bridge.switched
    reply = json.loads((controller.folder / "replies" / path.name).read_text(encoding="utf-8"))
    assert reply["message"] == runtime.tr("Control request rejected; verify its account pool and settings.")


def test_quota_poll_publishes_result_and_reuses_fresh_cache(tmp_path, monkeypatch):
    monitor = runtime.CodexQuotaMonitor(tmp_path, "local")
    calls = []
    monkeypatch.setattr(runtime.time, "time", lambda: 1000)
    def fetch(account, *_args):
        calls.append(account["id"])
        return quota(account["id"], 20)
    monkeypatch.setattr(runtime, "fetch_quota", fetch)
    monitor._poll(ACCOUNTS[0], 60)
    monitor._poll(ACCOUNTS[0], 60)
    assert calls == ["a"]
    value = json.loads(runtime.quota_file(tmp_path, "a", "local").read_text(encoding="utf-8"))
    assert value["account_id"] == "a"


def test_quota_failure_backoff_is_bounded_and_never_publishes_exception(tmp_path, monkeypatch):
    monitor = runtime.CodexQuotaMonitor(tmp_path, "local")
    clock = [1000.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    calls = []
    def fail(*_args):
        calls.append(1)
        raise RuntimeError("fixture-secret")
    monkeypatch.setattr(runtime, "fetch_quota", fail)
    delays = []
    for _ in range(9):
        monitor._poll(ACCOUNTS[0], 60)
        delays.append(monitor._next["a"] - clock[0])
        clock[0] = monitor._next["a"]
    assert delays[:3] == [120, 240, 480] and max(delays) <= 3600 and len(calls) == 9
    assert not runtime.quota_file(tmp_path, "a", "local").exists()
    assert not list((tmp_path / "quota").glob("*.lease"))
    assert "fixture-secret" not in runtime.quota_file(tmp_path, "a", "local").with_suffix(".backoff").read_text(encoding="utf-8")


def test_failure_cooldown_is_shared_by_every_terminal_of_the_environment(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    calls = []
    def fail(*_args):
        calls.append(1)
        raise RuntimeError("down")
    monkeypatch.setattr(runtime, "fetch_quota", fail)
    first, second = runtime.CodexQuotaMonitor(tmp_path, "local"), runtime.CodexQuotaMonitor(tmp_path, "local")
    first._poll(ACCOUNTS[0], 60)
    second._poll(ACCOUNTS[0], 60)
    assert len(calls) == 1 and second._next["a"] == 1120
    clock[0] = 1121
    second._poll(ACCOUNTS[0], 60)
    assert len(calls) == 2 and second._next["a"] == 1121 + 240      # the shared count keeps growing
    other = runtime.CodexQuotaMonitor(tmp_path, "remote-host")
    monkeypatch.setattr(runtime, "fetch_quota", lambda account, root, env: dict(quota("a", 10), environment_id=env))
    other._poll(ACCOUNTS[0], 60)                    # another environment has its own cool-down
    assert runtime.quota_file(tmp_path, "a", "remote-host").exists()


# --- recovery of the same account: only a verified reset and a later query continue the task ------------------------
def wait_on_exhausted_current(controller, bridge, when_empty="wait"):
    controller.command(command("policy", policy=policy(when_empty=when_empty).to_dict(), accounts=ACCOUNTS))
    bridge.data.update(last_error_kind="usageLimitExceeded", last_error_at=900)
    controller.monitor.values = {"a": quota("a", 100, 950), "b": quota("b", 100, 950)}
    assert controller.tick()["status"] == "waiting_quota"
    assert controller.next_retry == 1300 and controller.monitor.due == ({"a", "b"}, 1300)


def test_trusted_reset_and_later_query_continue_the_failed_task(session):
    controller, bridge, clock = session
    wait_on_exhausted_current(controller, bridge)
    clock[0] = 1310
    controller.monitor.values = {"a": quota("a", 0, 1305), "b": quota("b", 100, 950)}
    result = controller.tick()
    assert bridge.resumed == 1 and bridge.recovered is True
    assert result["status"] == "running" and not controller.waiting


def test_rising_percentage_before_the_reset_never_continues(session):
    controller, bridge, clock = session
    wait_on_exhausted_current(controller, bridge)
    clock[0] = 1100
    controller.monitor.values = {"a": quota("a", 0, 1090), "b": quota("b", 100, 950)}
    result = controller.tick()
    assert bridge.resumed == 0 and result["status"] == "idle"
    assert result["message"] == runtime.tr("Quota is available again; continue the task in the terminal when ready.")


def test_a_query_from_before_the_reset_never_continues(session):
    controller, bridge, clock = session
    wait_on_exhausted_current(controller, bridge)
    clock[0] = 1310
    # Fresh by the interval, but asked before the reset: not proof that the reset happened.
    controller.monitor.values = {"a": quota("a", 0, 1290), "b": quota("b", 100, 950)}
    controller.tick()
    assert bridge.resumed == 0


def test_notify_only_recovery_never_starts_work(session):
    controller, bridge, clock = session
    controller.command(command("policy", policy=policy(when_empty="notify-only").to_dict(), accounts=ACCOUNTS))
    bridge.data.update(last_error_kind="usageLimitExceeded", last_error_at=900)
    controller.monitor.values = {"a": quota("a", 100, 950), "b": quota("b", 100, 950)}
    result = controller.tick()
    assert result["status"] == "waiting_quota" and controller.next_retry is None
    clock[0] = 1310
    controller.monitor.values = {"a": quota("a", 0, 1305), "b": quota("b", 100, 950)}
    controller.tick()
    assert bridge.resumed == 0 and not controller.waiting


def test_recovery_waits_for_a_safe_checkpoint(session):
    controller, bridge, clock = session
    wait_on_exhausted_current(controller, bridge)
    clock[0] = 1310
    controller.monitor.values = {"a": quota("a", 0, 1305), "b": quota("b", 100, 950)}
    bridge.data.update(state="unknown", can_switch=False)
    controller.tick()
    assert bridge.resumed == 0 and controller.waiting
    bridge.data.update(state="idle", can_switch=True)
    controller.tick()
    assert bridge.resumed == 1


def test_wait_without_a_trusted_reset_has_no_retry(session):
    controller, bridge, _ = session
    controller.command(command("policy", policy=policy().to_dict(), accounts=ACCOUNTS))
    no_reset = quota("a", 100)
    no_reset["buckets"]["codex"]["windows"][0]["resets_at"] = None
    controller.monitor.values = {"a": no_reset, "b": quota("b", 100)}
    assert controller.tick()["status"] == "waiting_quota"
    # The other account may come back at its reset; the current one can never be resumed automatically.
    assert controller.next_retry == 1300 and controller.wait_reset is None
    assert controller.monitor.due == ({"a", "b"}, 1300)


def test_unknown_current_quota_never_moves_the_conversation(session):
    controller, bridge, _ = session
    controller.command(command("policy", policy=policy().to_dict(), accounts=ACCOUNTS))
    controller.monitor.values = {"b": quota("b", 0)}
    controller.tick()
    assert bridge.data["pending_account"] is None and not controller.waiting


def test_quota_failure_is_known_exhaustion_until_a_later_query(session):
    controller, bridge, _ = session
    controller.command(command("policy", policy=policy().to_dict(), accounts=ACCOUNTS))
    controller.monitor.values = {"a": quota("a", 10, 950), "b": quota("b", 20)}
    bridge.data.update(last_error_kind="usageLimitExceeded", last_error_at=990)
    controller.tick()
    assert bridge.switched == ["b"]


# --- structured events: sparse, each value with its own observation time --------------------------------------------
def test_newer_event_window_overrides_only_its_own_window(session):
    controller, bridge, clock = session
    controller.command(command("policy", policy=policy().to_dict(), accounts=ACCOUNTS))
    clock[0] = 1500
    controller.monitor.values = {"a": {**quota("a", 20, 1450), "buckets": {"codex": {"windows": [
        {"slot": "primary", "used_percent": 20, "resets_at": 2000},
        {"slot": "secondary", "used_percent": 20, "resets_at": 9000}]}}}, "b": quota("b", 20, 1450)}
    bridge.data.update(rate_limits={"limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": 2000}},
                       rate_limit_times={"limitId": 1490, "primary": 1490}, rate_limits_checked_at=1490)
    controller.tick()
    assert bridge.switched == ["b"]


def test_event_missing_a_window_never_refreshes_it():
    cached = {"tool": "codex", "account_id": "a", "environment_id": "local", "checked_at": 1000,
              "buckets": {"codex": {"windows": [{"slot": "primary", "used_percent": 0, "resets_at": 2000},
                                                {"slot": "secondary", "used_percent": 100, "resets_at": 9000}]}}}
    bridge = {"account": "a", "rate_limits": {"primary": {"usedPercent": 0, "resetsAt": 2000}},
              "rate_limit_times": {"primary": 1500}, "rate_limits_checked_at": 1500}
    merged = runtime.observed_quota(cached, bridge, "local")
    assert merged["checked_at"] == 1000
    assert {w["slot"]: w["used_percent"] for w in merged["buckets"]["codex"]["windows"]} == {"primary": 0, "secondary": 100}
    # Too old for a 60 s flow at 1500: unknown, not "available".
    assert runtime.quota_state(merged, "local", 1500, 60, 10) == (None, None)


def test_bridge_merges_sparse_updates_and_ignores_other_meters():
    bridge = runtime.ManagedCodexBridge(lambda *_: None, "a")
    bridge._observe({"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "limitId": "codex", "primary": {"usedPercent": 10}, "secondary": {"usedPercent": 20}}}})
    first = bridge.snapshot()["rate_limit_times"]["secondary"]
    bridge._observe({"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "limitId": "codex", "primary": {"usedPercent": 30}, "secondary": None, "rateLimitReachedType": None}}})
    bridge._observe({"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "limitId": "other-product", "primary": {"usedPercent": 100}}}})
    snapshot = bridge.snapshot()
    assert snapshot["rate_limits"]["primary"] == {"usedPercent": 30}
    assert snapshot["rate_limits"]["secondary"] == {"usedPercent": 20}
    assert snapshot["rate_limit_times"]["secondary"] == first
    assert "rateLimitReachedType" not in snapshot["rate_limits"]


# --- control requests ------------------------------------------------------------------------------------------------
def queue_command(controller, created, **values):
    identifier = "0" * 31 + str(len(list((controller.folder / "replies").glob("*"))))
    data = dict(command("switch", target_account_id="b", accounts=ACCOUNTS, **values), id=identifier, created=created)
    runtime.write_json(controller.folder / "commands" / (identifier + ".json"), data)
    return identifier


def test_queued_command_runs_once_and_expired_command_never_runs(session):
    controller, bridge, clock = session
    stale = queue_command(controller, clock[0] - runtime.CONTROL_COMMAND_MAX_AGE - 1)
    controller.tick()
    reply = json.loads((controller.folder / "replies" / (stale + ".json")).read_text(encoding="utf-8"))
    assert reply["accepted"] is False and not bridge.switched
    fresh = queue_command(controller, clock[0])
    controller.tick()
    controller.tick()
    reply = json.loads((controller.folder / "replies" / (fresh + ".json")).read_text(encoding="utf-8"))
    assert reply["accepted"] is True and bridge.switched == ["b"]


def test_store_metadata_is_rejected_by_runtime_but_filtered_by_the_service(tmp_path):
    from vibe_launcher.service import LauncherService
    from vibe_launcher.store import Store
    store = Store(tmp_path / "launcher.db")
    saved = store.save("accounts", {"id": "a", "name": "A", "tool": "codex", "auth": "subscription",
                                    "environment_id": "local", "credential_revision": "r1"})
    with pytest.raises(ValueError):
        runtime.validate_codex_control({"session_id": "s", "control_action": "status", "accounts": [saved]})
    clean = LauncherService(store).control_accounts([saved], "local")
    assert clean == [{key: saved[key] for key in runtime.CONTROL_ACCOUNT_FIELDS if key in saved}]
    runtime.validate_codex_control({"session_id": "s", "control_action": "status", "accounts": clean})


def test_status_needs_a_live_heartbeat(tmp_path, monkeypatch):
    folder = runtime.codex_control_path(tmp_path, "session")
    runtime.write_json(folder / "state.json", {"status": "idle", "environment_id": "local", "worker_pid": 1})
    monkeypatch.setattr(runtime, "process_alive", lambda pid: None)
    assert runtime.codex_control_status(tmp_path, "session", "local")["status"] == "needs_attention"
    runtime.write_json(folder / "heartbeat", {"pid": 1})
    assert runtime.codex_control_status(tmp_path, "session", "local")["status"] == "idle"
    old = runtime.time.time() - 120
    runtime.os.utime(folder / "heartbeat", (old, old))
    status = runtime.codex_control_status(tmp_path, "session", "local")
    assert status["status"] == "needs_attention" and status["worker_alive"] is False
    reply = runtime.codex_control_action({"session_id": "session", "control_action": "cancel", "environment_id": "local"},
                                         tmp_path)
    assert reply["accepted"] is False and not list((folder / "commands").glob("*.json"))


def test_exact_thread_owner_is_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "process_alive", lambda pid: None)
    folder = runtime.codex_control_path(tmp_path, "older")
    runtime.write_json(folder / "state.json", {"status": "idle", "thread_id": "thread-one", "environment_id": "local"})
    runtime.write_json(folder / "heartbeat", {})
    assert runtime.thread_owner_active(tmp_path, "thread-one", "newer")
    assert not runtime.thread_owner_active(tmp_path, "thread-two", "newer")
    runtime.write_json(folder / "state.json", {"status": "closed", "thread_id": "thread-one", "environment_id": "local"})
    assert not runtime.thread_owner_active(tmp_path, "thread-one", "newer")


# --- quota monitor ----------------------------------------------------------------------------------------------------
def test_reset_makes_an_earlier_answer_stale(tmp_path, monkeypatch):
    monitor = runtime.CodexQuotaMonitor(tmp_path, "local")
    calls = []
    monkeypatch.setattr(runtime.time, "time", lambda: 1000)
    monkeypatch.setattr(runtime, "fetch_quota", lambda account, *_: calls.append(1) or quota(account["id"], 20))
    monitor._poll(ACCOUNTS[0], 600)
    monitor.require_after(["a"], 1000.5)
    monitor._poll(ACCOUNTS[0], 600)
    assert len(calls) == 2


def test_answer_for_another_account_is_never_cached(tmp_path, monkeypatch):
    monitor = runtime.CodexQuotaMonitor(tmp_path, "local")
    monkeypatch.setattr(runtime, "fetch_quota", lambda account, *_: quota("b", 0))
    monitor._poll(ACCOUNTS[0], 60)
    assert not runtime.quota_file(tmp_path, "a", "local").exists() and monitor._failures["a"] == 1


def test_lease_of_a_dead_monitor_is_released(tmp_path, monkeypatch):
    monitor = runtime.CodexQuotaMonitor(tmp_path, "local")
    lease = runtime.quota_file(tmp_path, "a", "local").with_suffix(".lease")
    runtime.private_dir(lease.parent)
    lease.write_text("", encoding="utf-8")         # half-written by a monitor that died
    old = runtime.time.time() - runtime.QUOTA_LEASE_STALE - 5
    runtime.os.utime(lease, (old, old))
    calls = []
    monkeypatch.setattr(runtime, "fetch_quota", lambda account, *_: calls.append(1) or quota("a", 0, runtime.time.time()))
    monitor._poll(ACCOUNTS[0], 60)
    assert not lease.exists() and not calls
    monitor._poll(ACCOUNTS[0], 60)
    assert calls == [1]


# --- instruction sources ---------------------------------------------------------------------------------------------
def test_instruction_sources_compare_profile_files_by_content(tmp_path):
    first, second, project = tmp_path / "a", tmp_path / "b", tmp_path / "project"
    for folder in (first, second, project):
        folder.mkdir()
    (first / "AGENTS.md").write_text("same", encoding="utf-8")
    (second / "AGENTS.md").write_text("same", encoding="utf-8")
    (project / "AGENTS.md").write_text("project", encoding="utf-8")
    shared = str(project / "AGENTS.md")
    assert runtime.instruction_sources_match([str(first / "AGENTS.md"), shared], [str(second / "AGENTS.md"), shared], first, second)
    (second / "AGENTS.md").write_text("changed", encoding="utf-8")
    assert not runtime.instruction_sources_match([str(first / "AGENTS.md")], [str(second / "AGENTS.md")], first, second)
    assert not runtime.instruction_sources_match([shared], [], first, second)
    (first / "auth.json").write_text("{}", encoding="utf-8")
    assert not runtime.instruction_sources_match([str(first / "auth.json")], [str(first / "auth.json")], first, first)


# --- process lifecycle ------------------------------------------------------------------------------------------------
class StartFailure(runtime.ManagedCodexBridge):
    closed = []

    def start(self):
        raise OSError("listener unavailable")

    def close(self):
        StartFailure.closed.append(True)


def managed_request(tmp_path):
    return {"session_id": "session", "environment_id": "local", "account": ACCOUNTS[0], "path": str(tmp_path)}


def test_unsupported_cli_runs_unmanaged_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: "codex")
    assert runtime.run_managed_codex(managed_request(tmp_path), tmp_path, {}, tmp_path / "stop") is None
    state = json.loads((runtime.codex_control_path(tmp_path, "session") / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "unmanaged"


def test_start_failure_closes_backend_and_controller(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: "codex")
    monkeypatch.setattr(runtime, "managed_codex_supported", lambda *args: True)
    monkeypatch.setattr(runtime, "ManagedCodexBridge", StartFailure)
    StartFailure.closed = []
    (tmp_path / "stop").write_text("", encoding="utf-8")
    with pytest.raises(OSError):
        runtime.run_managed_codex(managed_request(tmp_path), tmp_path, {}, tmp_path / "stop")
    assert StartFailure.closed == [True] and not (tmp_path / "stop").exists()
    state = json.loads((runtime.codex_control_path(tmp_path, "session") / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "closed"


def test_frontend_failure_stops_the_started_backend(tmp_path, monkeypatch):
    import subprocess
    import sys
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: "codex")
    monkeypatch.setattr(runtime, "managed_codex_supported", lambda *args: True)
    backends = []
    popen = subprocess.Popen

    def backend(command, **kwargs):
        if command[1] == "--remote":
            raise FileNotFoundError("frontend missing")
        process = popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        backends.append(process)
        return process
    monkeypatch.setattr(runtime.subprocess, "Popen", backend)
    monkeypatch.setattr(runtime, "prepare", lambda account, root: {})
    with pytest.raises(FileNotFoundError):
        runtime.run_managed_codex(managed_request(tmp_path), tmp_path, {}, tmp_path / "stop")
    assert len(backends) == 1 and backends[0].wait(timeout=10) is not None


# --- environment-level state: approval rules, loaded skills and connected apps ----------------------------------------
RULE_A = b'prefix_rule(pattern=["git", "status"], decision="allow")\n'
RULE_B = b'# approved\nprefix_rule(pattern=["npm", "test"], decision="allow")\n'


def rules_session(controller, bridge, tmp_path, monkeypatch):
    controller.accounts.update({a["id"]: a for a in ACCOUNTS})
    for account, rule in (("a", RULE_A), ("b", RULE_B)):
        runtime._write_private(runtime.profile_dir(tmp_path, ACCOUNTS[0 if account == "a" else 1]) / "rules" / "default.rules", rule)
    monkeypatch.setattr(runtime, "prepare", lambda *args: {})
    monkeypatch.setattr(runtime, "check_login", lambda *args: True)
    monkeypatch.setattr(runtime, "sync_user_data", lambda *args, **kwargs: {})
    monkeypatch.setattr(controller, "_check_loaded_capabilities", lambda *args: [])
    monkeypatch.setattr(controller, "_check_mcp_logins", lambda *args: [])
    return [runtime.profile_dir(tmp_path, account) / "rules" / "default.rules" for account in ACCOUNTS]


def test_differing_approval_rules_block_without_writing_either_profile(session, tmp_path, monkeypatch):
    """Regression: the switch used to merge both profiles' allow rules before it could be refused or cancelled."""
    controller, bridge, _ = session
    paths = rules_session(controller, bridge, tmp_path, monkeypatch)
    for _ in range(2):
        with pytest.raises(runtime.CodexHandoffBlocked) as blocked:
            controller.validate_target("b", {})
        assert blocked.value.gaps == [{"kind": "rules", "name": "default.rules"}]
    controller.command(command("cancel"))
    assert [path.read_bytes() for path in paths] == [RULE_A, RULE_B]
    assert runtime.codex_rule_differences(paths[0].parent.parent, paths[1].parent.parent) == ["default.rules"]


def test_a_flow_that_allows_gaps_cannot_skip_differing_rules(session, tmp_path, monkeypatch):
    controller, bridge, _ = session
    paths = rules_session(controller, bridge, tmp_path, monkeypatch)
    accepted = {"b": ["plugin:pages", "app:docs", "mcp:docs", "mcp_unknown:docs"]}    # rules cannot be accepted
    controller.command(command("policy", policy=policy(accepted_gaps=accepted).to_dict(), accounts=ACCOUNTS))
    with pytest.raises(runtime.CodexHandoffBlocked):
        controller.validate_target("b", {})
    assert [path.read_bytes() for path in paths] == [RULE_A, RULE_B]


def test_confirmed_alignment_copies_current_rules_once_and_keeps_a_backup(session, tmp_path, monkeypatch):
    controller, bridge, _ = session
    paths = rules_session(controller, bridge, tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "codex_profile_fingerprint", lambda *args: {})
    controller.command(command("switch", target_account_id="b", accounts=ACCOUNTS, align_rules=True))
    controller.validate_target("b", {})
    assert [path.read_bytes() for path in paths] == [RULE_A, RULE_A]
    backups = list((tmp_path / "rules-backups" / "b").rglob("default.rules"))
    assert len(backups) == 1 and backups[0].read_bytes() == RULE_B
    runtime._write_private(paths[1], RULE_B)
    with pytest.raises(runtime.CodexHandoffBlocked):       # one confirmation aligns once
        controller.validate_target("b", {})


def test_alignment_waits_while_another_terminal_uses_the_target(session, tmp_path, monkeypatch):
    controller, bridge, _ = session
    paths = rules_session(controller, bridge, tmp_path, monkeypatch)
    other = runtime.codex_control_path(tmp_path, "other")
    runtime.write_json(other / "state.json", {"status": "idle", "account_id": "b", "worker_pid": 1})
    runtime.write_json(other / "heartbeat", {})
    runtime.os.utime(other / "heartbeat", (1000, 1000))         # fresh at the fixture's clock
    monkeypatch.setattr(runtime, "process_alive", lambda pid: True)
    controller.command(command("switch", target_account_id="b", accounts=ACCOUNTS, align_rules=True))
    with pytest.raises(runtime.CodexControlError, match="another terminal"):
        controller.validate_target("b", {})
    assert [path.read_bytes() for path in paths] == [RULE_A, RULE_B]


def test_align_rules_is_only_a_switch_choice():
    with pytest.raises(ValueError):
        runtime.validate_codex_control({"session_id": "s", "control_action": "policy", "align_rules": True, "accounts": []})
    with pytest.raises(ValueError):
        runtime.validate_codex_control({"session_id": "s", "control_action": "switch", "target_account_id": "b",
                                        "align_rules": "yes", "accounts": []})


def test_fingerprint_leaves_plugin_caches_and_rules_to_their_own_checks(tmp_path):
    for account, version in (("a", "1.0"), ("b", "2.0")):
        profile = runtime.profile_dir(tmp_path, {"id": account, "tool": "codex"})
        runtime._write_private(profile / "plugins" / "cache" / "x" / version / "README.md", version.encode())
        runtime._write_private(profile / "rules" / "default.rules", version.encode())
        runtime._write_private(profile / "AGENTS.md", b"same")
    assert (runtime.codex_profile_fingerprint({"id": "a", "tool": "codex"}, tmp_path)
            == runtime.codex_profile_fingerprint({"id": "b", "tool": "codex"}, tmp_path))


def loaded(tmp_path, skills, apps, plugins=None, background=False):
    for name, text in skills.items():
        runtime._write_private(tmp_path / "skills" / name / "SKILL.md", text.encode())
    plugin_skill = {"name": "background", "path": str(tmp_path / "missing"), "scope": "user", "enabled": True,
                    "description": "d", "pluginId": "late@remote"}        # loads asynchronously: never compared
    return ({"data": [{"cwd": "p", "errors": [], "skills": [
        {"name": name, "path": str(tmp_path / "skills" / name / "SKILL.md"), "scope": "user", "enabled": True,
         "description": "d"} for name in skills] + ([plugin_skill] if background else [])}]},
            {"marketplaces": [{"name": "m", "plugins": [{"id": key, "installed": True, "enabled": True, "localVersion": value}
                                                       for key, value in (plugins or {}).items()]}]},
            {"apps": [{"id": key, "runtimeName": key.title(), "enabled": True, "callable": True} for key in apps]})


@pytest.mark.parametrize("target_skills, target_apps, target_plugins, problem, gaps", [
    ({"review": "v1", "extra": "x"}, ["github", "calendar"], {"pages@remote": "1", "more@remote": "2"}, None, []),
    ({"review": "v2"}, ["github"], {"pages@remote": "1"}, "Skills differ", None),
    ({}, ["github"], {"pages@remote": "1"}, "Skills differ", None),
    ({"review": "v1"}, [], {"pages@remote": "1"}, None, [{"kind": "app", "name": "Github"}]),
    ({"review": "v1"}, ["github"], {}, None, [{"kind": "plugin", "name": "pages"}]),
    ({"review": "v1"}, ["github"], {"pages@remote": "0.9"}, None, [{"kind": "plugin", "name": "pages"}]),
])
def test_switch_checks_every_loaded_skill_plugin_and_connected_app(session, tmp_path, monkeypatch, target_skills,
                                                                  target_apps, target_plugins, problem, gaps):
    """Skill content must match; missing plugins and apps are account differences the user resolves."""
    controller, bridge, _ = session
    source = loaded(tmp_path / "source", {"review": "v1"}, ["github"], {"pages@remote": "1"}, background=True)
    target = loaded(tmp_path / "target", target_skills, target_apps, target_plugins)
    answers = {"skills/list": source[0], "plugin/installed": source[1], "app/installed": source[2]}
    bridge.request = lambda method, params: answers[method]
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: "codex")
    monkeypatch.setattr(runtime, "rpc_requests", lambda *args, **kwargs: list(target))
    monkeypatch.setattr(runtime, "_MESSAGES", {})
    if problem is None:
        assert controller._check_loaded_capabilities(ACCOUNTS[1], {}) == gaps
    else:
        with pytest.raises(runtime.CodexControlError, match=problem):
            controller._check_loaded_capabilities(ACCOUNTS[1], {})


@pytest.mark.parametrize("choice", ["none", "command", "policy", "partly"])
def test_account_differences_block_until_the_user_chooses(session, tmp_path, monkeypatch, choice):
    controller, bridge, _ = session
    controller.accounts.update({a["id"]: a for a in ACCOUNTS})
    bridge.data["account"] = "a"
    monkeypatch.setattr(runtime, "prepare", lambda *args: {})
    monkeypatch.setattr(runtime, "check_login", lambda *args: True)
    monkeypatch.setattr(runtime, "sync_user_data", lambda *args, **kwargs: {})
    monkeypatch.setattr(runtime, "codex_rule_differences", lambda *args: [])
    monkeypatch.setattr(runtime, "codex_profile_fingerprint", lambda *args: {})
    monkeypatch.setattr(controller, "_check_loaded_capabilities", lambda *args: [{"kind": "plugin", "name": "pages"}])
    monkeypatch.setattr(controller, "_check_mcp_logins", lambda *args: [{"kind": "mcp", "name": "docs"}])
    if choice == "command":
        controller.command(command("switch", target_account_id="b", accounts=ACCOUNTS, allow_gaps=True))
    elif choice == "policy":
        accepted = {"b": ["plugin:pages", "mcp:docs"]}
        controller.command(command("policy", policy=policy(accepted_gaps=accepted).to_dict(), accounts=ACCOUNTS))
    elif choice == "partly":
        # The plan accepted the plugin; the MCP sign-in appeared later (nobody accepted it): the switch stops.
        controller.command(command("policy", policy=policy(accepted_gaps={"b": ["plugin:pages"]}).to_dict(), accounts=ACCOUNTS))
    if choice in ("none", "partly"):
        with pytest.raises(runtime.CodexHandoffBlocked) as blocked:
            controller.validate_target("b", {})
        assert blocked.value.gaps == [{"kind": "plugin", "name": "pages"}, {"kind": "mcp", "name": "docs"}]
    else:
        controller.validate_target("b", {})
        assert controller._gaps_accepted[0]["name"] == "pages"


@pytest.mark.parametrize("accepted", [{"b": ["rules:default.rules"]}, {"b": "plugin:pages"}, {"b": ["plugin:" + "x" * 65]},
                                      {"../b": ["plugin:pages"]}, ["plugin:pages"]])
def test_policy_accepts_only_account_level_differences_by_name(accepted):
    with pytest.raises(ValueError):
        policy(accepted_gaps=accepted)
    assert policy().accepted_gaps is None
    kept = policy(accepted_gaps={"b": ["mcp:docs", "plugin:pages", "plugin:pages"], "a": []})
    assert runtime.QuotaPolicy.from_dict(kept.to_dict()).accepted_gaps == {"b": ["mcp:docs", "plugin:pages"]}


def test_mcp_status_codex_cannot_determine_is_left_to_the_user(session, monkeypatch):
    controller, bridge, _ = session
    bridge.request = lambda method, params: {"data": [{"name": "remote", "authStatus": "unknown"},
                                                      {"name": "local", "authStatus": "unsupported"}]}
    assert controller._check_mcp_logins(ACCOUNTS[1], {}) == [{"kind": "mcp_unknown", "name": "remote"}]
    bridge.request = lambda method, params: {"data": [{"name": "odd", "authStatus": "something-new"}]}
    with pytest.raises(runtime.CodexControlError):
        controller._check_mcp_logins(ACCOUNTS[1], {})


def test_a_block_report_is_applied_once_so_a_new_flow_can_decide_again(session):
    controller, bridge, clock = session
    controller.command(command("policy", policy=policy().to_dict(), accounts=ACCOUNTS))
    controller.monitor.values = {"a": quota("a", 100), "b": quota("b", 20)}
    bridge.data["blocked"] = {"target": "b", "gaps": [{"kind": "plugin", "name": "pages"}], "at": 1}
    controller.tick()
    assert not bridge.switched and controller._skip["b"] > clock[0]
    controller.command(command("policy", policy=policy(accepted_gaps={"b": ["plugin:pages"]}).to_dict(), accounts=ACCOUNTS))
    controller.tick()                       # the same old report must not skip "b" again
    assert bridge.switched == ["b"]


# --- simulated quota (scripts/simulate_quota.py) -----------------------------------------------------------------------
def simulate(controller, clock, accounts, fail_once=False, minutes=10, **extra):
    data = {"label": "test", "expires_at": clock[0] + minutes * 60, "quota_failure_once": fail_once,
            "accounts": {key: {"windows": [{"slot": "primary", "used_percent": used, "resets_at": reset}], **extra.get(key, {})}
                         for key, (used, reset) in accounts.items()}}
    runtime.write_json(controller.folder / "simulated-quota.json", data)


@pytest.fixture
def real_time_file(session, monkeypatch):
    """The simulation's age is checked against the file's real mtime: keep the fake clock near real time."""
    controller, bridge, clock = session
    clock[0] = runtime.datetime.now().timestamp()       # time.time itself is the fake clock here
    return controller, bridge, clock


def test_simulated_low_quota_drives_the_normal_switch(real_time_file):
    controller, bridge, clock = real_time_file
    controller.command(command("policy", policy=policy().to_dict(), accounts=ACCOUNTS))
    simulate(controller, clock, {"a": (97, clock[0] + 3600), "b": (10, clock[0] + 3600)})
    result = controller.tick()
    assert bridge.switched == ["b"] and result["simulated"] is True


def test_simulated_failure_applies_once_and_continues_after_the_switch(real_time_file):
    controller, bridge, clock = real_time_file
    controller.command(command("policy", policy=policy().to_dict(), accounts=ACCOUNTS))
    bridge.data.update(state="running", can_switch=False)
    simulate(controller, clock, {"a": (100, clock[0] + 3600), "b": (10, clock[0] + 3600)}, fail_once=True)
    controller.tick()
    assert bridge.data["last_error_kind"] is None              # not at a settled point yet
    bridge.data.update(state="idle", can_switch=True)
    controller.tick()
    controller.tick()
    assert bridge.switched == ["b"] and bridge.resumed == 1
    saved = json.loads((controller.folder / "simulated-quota.json").read_text(encoding="utf-8"))
    assert saved["quota_failure_once"] == "used"


def test_simulated_reset_recovers_the_same_account(real_time_file):
    controller, bridge, clock = real_time_file
    controller.command(command("policy", policy=policy().to_dict(), accounts=ACCOUNTS))
    reset = clock[0] + 90
    simulate(controller, clock, {"a": (100, reset), "b": (100, reset + 600)}, fail_once=True,
             a={"after": {"at": reset, "windows": [{"slot": "primary", "used_percent": 5, "resets_at": reset + 18000}]}})
    controller.tick()
    assert controller.tick()["status"] == "waiting_quota" and controller.wait_reset == reset
    clock[0] = reset + 1
    controller.tick()
    assert bridge.resumed == 1 and bridge.recovered is True


@pytest.mark.parametrize("content", [{"expires_at": 1}, {"expires_at": "soon"}, [], "x"])
def test_expired_or_malformed_simulation_is_ignored(real_time_file, content):
    controller, bridge, clock = real_time_file
    controller.command(command("policy", policy=policy().to_dict(), accounts=ACCOUNTS))
    runtime.write_json(controller.folder / "simulated-quota.json", content)
    controller.monitor.values = {"a": quota("a", 10, clock[0]), "b": quota("b", 10, clock[0])}
    result = controller.tick()
    assert not bridge.switched and result["simulated"] is False


def test_a_simulation_cannot_outlive_an_hour(real_time_file):
    controller, bridge, clock = real_time_file
    controller.command(command("policy", policy=policy().to_dict(), accounts=ACCOUNTS))
    simulate(controller, clock, {"a": (97, clock[0] + 3600), "b": (10, clock[0] + 3600)}, minutes=120)
    assert controller.tick()["simulated"] is False and not bridge.switched


def test_trusted_reset_survives_the_stale_observation_right_after_it(session):
    """At the reset the cache still says "exhausted" with a reset in the past until the next query arrives."""
    controller, bridge, clock = session
    wait_on_exhausted_current(controller, bridge)
    clock[0] = 1300.5
    controller.monitor.values = {"a": quota("a", 100, 1290), "b": quota("b", 100, 1290)}
    controller.tick()
    assert controller.waiting and controller.wait_reset == 1300
    clock[0] = 1310
    controller.monitor.values = {"a": quota("a", 0, 1305), "b": quota("b", 100, 1290)}
    controller.tick()
    assert bridge.resumed == 1 and bridge.recovered is True


# --- a changed or removed flow withdraws its automatic actions; a manual choice stays ---------------------------------
def queue_automatic_switch(controller, bridge):
    controller.command(command("policy", policy=policy().to_dict(), accounts=ACCOUNTS))
    controller.monitor.values = {"a": quota("a", 100), "b": quota("b", 20)}
    bridge.data.update(state="running", can_switch=False, last_error_kind="usageLimitExceeded")
    controller.tick()
    assert bridge.data["pending_account"] == "b"


@pytest.mark.parametrize("change", [None, "disabled", "other"])
def test_disabling_or_replacing_the_flow_cancels_its_queued_switch(session, change):
    """Regression: a disabled flow still switched the account (and continued the task) at the next safe point."""
    controller, bridge, _ = session
    queue_automatic_switch(controller, bridge)
    replacement = {None: None, "disabled": policy(enabled=False).to_dict(),
                   "other": policy(account_ids=["a"], strategy="round-robin").to_dict()}[change]
    controller.command(command("policy", policy=replacement, accounts=ACCOUNTS))
    assert bridge.data["pending_account"] is None
    controller.monitor.values = {}
    bridge.data.update(state="idle", can_switch=True)
    controller.tick()
    assert bridge.switched == [] and bridge.resumed == 0


def test_a_handoff_already_running_finishes_but_does_not_continue_the_task(session):
    controller, bridge, _ = session
    queue_automatic_switch(controller, bridge)
    def busy():
        raise runtime.CodexControlError("Handoff is already in progress")
    bridge.cancel_switch = busy
    status = controller.command(command("policy", policy=None, accounts=[]))
    assert "after the handoff" in status["message"]
    bridge.data.update(state="idle", can_switch=True)
    controller.tick()
    assert bridge.switched == ["b"] and bridge.resumed == 0 and controller.notice == ""


def test_a_manual_switch_survives_a_flow_change_and_continues(session):
    controller, bridge, _ = session
    controller.command(command("policy", policy=policy().to_dict(), accounts=ACCOUNTS))
    bridge.data.update(state="running", can_switch=False, last_error_kind="usageLimitExceeded")
    controller.command(command("switch", target_account_id="b", accounts=ACCOUNTS))
    controller.command(command("policy", policy=None, accounts=[]))
    assert bridge.data["pending_account"] == "b"
    bridge.data.update(state="idle", can_switch=True)
    controller.tick()
    assert bridge.switched == ["b"] and bridge.resumed == 1


def test_a_terminal_nobody_shows_pauses_its_flow(session, monkeypatch):
    controller, bridge, _ = session
    queue_automatic_switch(controller, bridge)
    monkeypatch.setattr(controller, "_terminal_attached", lambda: False)
    status = controller.tick()
    assert bridge.data["pending_account"] is None and controller.monitor.configured is None
    assert status["paused"] and "paused" in status["message"]
    bridge.data.update(state="idle", can_switch=True)
    controller.tick()
    assert bridge.switched == []
    monkeypatch.setattr(controller, "_terminal_attached", lambda: True)
    controller.tick()                                       # the terminal is shown again: the flow resumes
    assert controller.monitor.configured is controller.policy and not controller.status()["paused"]
    controller.tick()
    assert bridge.switched == ["b"]


@pytest.mark.parametrize("output, code, attached", [
    (b"@1 0\n@2 1\n", 0, False), (b"@1 0\n@1 1\n", 0, True), (b"@1 2\n", 0, True), (b"", 1, False)])
def test_tmux_clients_decide_whether_the_terminal_is_shown(session, monkeypatch, output, code, attached):
    controller, _, _ = session
    monkeypatch.setattr(runtime, "WINDOWS", False)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/vibe-launcher,1,0")
    monkeypatch.setattr(runtime, "tmux_windows", lambda: {"session": "@1"})
    monkeypatch.setattr(runtime, "tmux", lambda *args, **kwargs: runtime.subprocess.CompletedProcess(args, code, output))
    assert controller._terminal_attached() is attached


def test_round_robin_progress_is_reported_and_restored(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "CodexQuotaMonitor", FakeMonitor)
    request = {"session_id": "session", "environment_id": "local", "account": ACCOUNTS[0], "path": str(tmp_path),
               "quota_policy": policy(strategy="round-robin").to_dict(), "quota_accounts": ACCOUNTS, "quota_cursor": "b"}
    controller = runtime.ManagedCodexSession(request, tmp_path, FakeBridge())
    try:
        assert controller.cursor == "b" and controller.status()["cursor"] == "b"
        controller.command(command("policy", policy=policy(account_ids=["a"]).to_dict(), accounts=ACCOUNTS))
        assert controller.cursor is None                    # a pool without that account starts over
    finally:
        controller._closed.set()


# --- Auto continue: the same account after its reset; a quota failure belongs to the account it happened on ----------
CLAUDE = [dict(account, tool="claude") for account in ACCOUNTS]


def continue_policy(accounts=("a",)):
    from vibe_launcher.quota_policy import continue_template
    return policy(account_ids=list(accounts), flow=continue_template())


def status_line(identifier, used, checked=900, reset=1300):
    """Claude's quota as its status line records it: no buckets, written only while the CLI runs."""
    return {"tool": "claude", "account_id": identifier, "environment_id": "local", "checked_at": checked, "reached": None,
            "windows": [{"slot": "five_hour", "used_percent": used, "window_minutes": 300, "resets_at": reset}]}


def fail(bridge, at):
    bridge.data.update(state="idle", can_switch=True, last_turn_status="failed", last_error_kind="usageLimitExceeded",
                       last_error_at=at)


@pytest.fixture
def claude(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    monkeypatch.setattr(runtime, "CodexQuotaMonitor", FakeMonitor)
    bridge = FakeBridge()
    controller = runtime.ManagedClaudeSession({"session_id": "session", "environment_id": "local",
        "account": CLAUDE[0], "path": str(tmp_path)}, tmp_path, bridge)
    controller.command(command("policy", policy=continue_policy().to_dict(), accounts=CLAUDE[:1]))
    yield controller, bridge, clock
    controller._closed.set()


def test_claude_continues_by_itself_and_the_launcher_only_follows(claude):
    controller, bridge, clock = claude
    controller.monitor.values = {"a": status_line("a", 100)}             # written before the failure
    fail(bridge, 1000)
    result = controller.tick()
    assert result["status"] == "waiting_quota" and controller.wait_reset == 1300
    assert result["message"] == "Waiting for quota to recover; the task continues automatically."
    clock[0] = 1302
    bridge.data.update(state="running", can_switch=False)                # Claude Code's own continuation at the reset
    result = controller.tick()
    assert result["status"] == "running" and not controller.waiting and bridge.resumed == 0
    bridge.data.update(state="idle", can_switch=True, last_turn_status="completed", last_error_kind=None,
                       last_error_at=None)
    clock[0] = 1400
    assert controller.tick()["status"] == "idle" and not controller.waiting and bridge.resumed == 0


def test_claude_that_did_not_continue_by_itself_is_left_to_the_user(claude):
    controller, bridge, clock = claude
    controller.monitor.values = {"a": status_line("a", 100)}
    fail(bridge, 1000)
    controller.tick()
    clock[0] = 1300 + 119
    controller.tick()
    assert controller.waiting and controller.next_retry == 1300          # its own wait may still be on the way
    clock[0] = 1300 + 121                                                # automatic continue off, cancelled or asleep
    result = controller.tick()
    assert result["status"] == "waiting_quota" and controller.next_retry is None
    assert result["message"] == "Quota ran out; continue the task in the terminal when quota is available."
    clock[0] = 5000
    controller.tick()
    assert bridge.resumed == 0                                           # the launcher never restarts Claude for it
    bridge.data.update(state="running", can_switch=False)                # you continue
    controller.tick()
    assert not controller.waiting


def test_claude_without_a_reported_reset_is_not_promised_to_continue(claude):
    controller, bridge, clock = claude
    fail(bridge, 1000)                                                   # no status line windows at all
    result = controller.tick()
    assert result["status"] == "waiting_quota" and controller.wait_reset is None
    assert result["message"] == "Quota ran out; continue the task in the terminal when quota is available."
    clock[0] = 100000
    controller.tick()
    assert bridge.resumed == 0 and controller.waiting


def test_a_new_limit_after_a_continuation_waits_for_its_own_reset(claude):
    controller, bridge, clock = claude
    controller.monitor.values = {"a": status_line("a", 100)}
    fail(bridge, 1000)
    controller.tick()
    clock[0] = 1301
    bridge.data.update(state="running", can_switch=False)                # continued (by Claude Code or by you) ...
    controller.tick()
    controller.monitor.values = {"a": status_line("a", 100, checked=1340, reset=2000)}
    fail(bridge, 1340)                                                   # ... and stopped by the limit again
    clock[0] = 1345
    controller.tick()
    assert controller.waiting and controller.wait_reset == 2000 and bridge.resumed == 0


def test_codex_auto_continue_resumes_only_after_a_query_made_after_the_reset(session):
    controller, bridge, clock = session
    controller.command(command("policy", policy=continue_policy().to_dict(), accounts=ACCOUNTS[:1]))
    controller.monitor.values = {"a": quota("a", 100, checked=990)}
    bridge.data.update(last_error_kind="usageLimitExceeded", last_error_at=1000, last_turn_status="failed")
    assert controller.tick()["status"] == "waiting_quota" and controller.wait_reset == 1300
    clock[0] = 1301
    controller.tick()
    assert bridge.resumed == 0                                           # nothing was asked after the reset yet
    controller.monitor.values = {"a": quota("a", 10, checked=1302)}
    clock[0] = 1303
    controller.tick()
    assert bridge.resumed == 1 and bridge.recovered is True and not controller.waiting
    clock[0] = 1320
    controller.tick()
    assert bridge.resumed == 1 and not controller.waiting                # the continued failure is over


def test_a_handoff_ends_the_old_accounts_quota_failure(session):
    controller, bridge, clock = session
    controller.command(command("policy", policy=policy().to_dict(), accounts=ACCOUNTS))
    controller.monitor.values = {"a": quota("a", 100, checked=990), "b": quota("b", 20, checked=990)}
    bridge.data.update(last_error_kind="usageLimitExceeded", last_error_at=1000, last_turn_status="failed")
    controller.tick()                                                    # a ran out: hand off to b and continue there
    assert bridge.switched == ["b"] and bridge.resumed == 1
    clock[0] = 1010
    controller.tick()                                                    # b is fine; a's failure is not b's
    assert not controller.waiting and bridge.data["pending_account"] is None and bridge.switched == ["b"]


# --- managed claude ---------------------------------------------------------------------------------------------------
REAL_SUPPORTED = runtime.managed_claude_supported      # conftest replaces it for every test

ACCOUNT_A = {"id": "account-a", "name": "Account A", "tool": "claude", "auth": "subscription", "environment_id": "local"}
ACCOUNT_B = {"id": "account-b", "name": "Account B", "tool": "claude", "auth": "subscription", "environment_id": "local"}


def hook(monkeypatch, kind, data, **env):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(data).encode())))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert runtime.claude_hook([kind]) == 0


def test_hook_records_only_the_fields_it_needs(tmp_path, monkeypatch):
    events = tmp_path / "events"
    hook(monkeypatch, "event", {"hook_event_name": "Stop", "session_id": "s", "background_tasks": [{"id": 1}],
                                "last_assistant_message": "secret answer", "tool_input": {"command": "rm"}},
         VIBE_CLAUDE_CONTROL=str(events), VIBE_CLAUDE_GENERATION="3")
    [path] = list(events.glob("*.json"))
    event = json.loads(path.read_text(encoding="utf-8"))
    assert event == {"hook_event_name": "Stop", "session_id": "s", "background_tasks": 1, "generation": "3"}


def test_hook_never_fails_the_cli(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"not json")))
    assert runtime.claude_hook(["event"]) == 0


def test_status_line_quota_is_cached_like_other_quota(tmp_path, monkeypatch):
    path = tmp_path / "quota" / "account-a__local.json"
    limits = {"five_hour": {"used_percentage": 23.5, "resets_at": 4102444800},
              "seven_day": {"used_percentage": 41, "resets_at": 4102444900}}
    hook(monkeypatch, "status", {"rate_limits": limits}, VIBE_CLAUDE_QUOTA=str(path), VIBE_CLAUDE_ACCOUNT="account-a",
         VIBE_CLAUDE_ENVIRONMENT="local")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot["tool"] == "claude" and [w["used_percent"] for w in snapshot["windows"]] == [23.5, 41]
    moment = runtime._quota_timestamp(snapshot["checked_at"])
    assert runtime.quota_state(snapshot, "local", moment + 30, 60, 10)[0] == 59
    assert runtime.quota_state(snapshot, "local", moment + 3700, 60, 10) == (None, None)     # an hour old: unknown
    snapshot["windows"][0]["resets_at"] = int(moment) - 1                                        # a window has reset
    assert runtime.quota_state(snapshot, "local", moment + 30, 60, 10) == (None, None)


def test_settings_add_hooks_and_wrap_the_users_status_line(tmp_path):
    settings = runtime.claude_settings("helper", {"command": "x", "padding": 2})
    assert set(settings["hooks"]) == set(runtime.CLAUDE_EVENTS)
    entry = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    # Work-starting events block until recorded, and a hook that fails or times out blocks the action.
    assert entry == {"type": "command", "command": "helper event PreToolUse", "timeout": 10, "onFailure": "block"}
    for name in ("UserPromptSubmit", "SessionStart", "SubagentStart", "PermissionRequest"):
        assert settings["hooks"][name][0]["hooks"][0]["onFailure"] == "block"
    assert settings["hooks"]["Stop"][0]["hooks"][0]["async"] is True
    assert settings["statusLine"] == {"type": "command", "command": "helper status", "padding": 2}
    project, profile = tmp_path / "project", tmp_path / "profile"
    (project / ".claude").mkdir(parents=True)
    profile.mkdir()
    assert runtime.claude_user_statusline(profile, project) is None
    (profile / "settings.json").write_text('{"statusLine": {"type": "command", "command": "user line"}}', encoding="utf-8")
    assert runtime.claude_user_statusline(profile, project)["command"] == "user line"
    (project / ".claude" / "settings.json").write_text('{"statusLine": {"type": "command", "command": "project line"}}', encoding="utf-8")
    assert runtime.claude_user_statusline(profile, project)["command"] == "project line"


def test_status_hook_records_quota_and_still_shows_the_users_line(tmp_path, monkeypatch, capfdbinary):
    path = tmp_path / "quota.json"
    limits = {"five_hour": {"used_percentage": 3, "resets_at": 4102444800}}
    hook(monkeypatch, "status", {"rate_limits": limits}, VIBE_CLAUDE_QUOTA=str(path), VIBE_CLAUDE_STATUSLINE="echo vibe-user-line")
    assert path.exists() and b"vibe-user-line" in capfdbinary.readouterr().out


def test_hook_command_runs_in_bash_and_powershell_without_quotes():
    command = selfcmd.hook_command("claude_hook")
    assert command.endswith("vibe_launcher.claude_hook") or command.endswith("claude_hook")
    assert "\\" not in command and (" " not in command.split(" -I ")[0] or command.startswith("powershell"))


def test_transcript_and_its_files_move_to_the_target_profile(tmp_path):
    source, target = tmp_path / "a", tmp_path / "b"
    folder = source / "projects" / "C--work"
    runtime._write_private(folder / "s1.jsonl", b'{"type":"user"}\n')
    runtime._write_private(folder / "s1" / "subagents" / "agent.jsonl", b"{}\n")
    runtime._write_private(source / "file-history" / "s1" / "v1", b"old")
    runtime._write_private(source / "todos" / "s1-agent.json", b"[]")
    runtime._write_private(target / "projects" / "C--work" / "s1.jsonl", b"stale")
    runtime.copy_claude_session(source, target, "s1")
    assert (target / "projects" / "C--work" / "s1.jsonl").read_bytes() == b'{"type":"user"}\n'
    assert (target / "projects" / "C--work" / "s1" / "subagents" / "agent.jsonl").exists()
    assert (target / "file-history" / "s1" / "v1").exists() and (target / "todos" / "s1-agent.json").exists()
    with pytest.raises(runtime.CodexControlError):
        runtime.copy_claude_session(source, target, "missing")


def test_account_level_capabilities_are_read_by_name_only(tmp_path):
    profile, project = tmp_path / "p", tmp_path / "work"
    runtime._write_private(profile / "settings.json", json.dumps({"enabledPlugins": {"a@m": True, "b@m": False}}).encode())
    runtime._write_private(profile / ".claude.json", json.dumps({
        "mcpServers": {"docs": {"type": "http", "url": "https://example.com", "headers": {"Authorization": "x"}},
                       "local": {"command": "node"}},
        "projects": {str(project).replace("\\", "/"): {"mcpServers": {"db": {"type": "sse"}}}}}).encode())
    plugins, servers = runtime.claude_capabilities(profile, project)
    assert plugins == {"a@m"} and servers == {"docs": "http", "local": "stdio", "db": "sse"}


class Process:
    def __init__(self):
        self.code = None
        self.pid = 4321
    def poll(self):
        return self.code
    def wait(self, timeout=None):
        return self.code


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    spawned = []

    def popen(argv, **kwargs):
        spawned.append((argv, kwargs["env"]["VIBE_CLAUDE_GENERATION"]))
        return Process()
    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    monkeypatch.setattr(runtime, "prepare", lambda account, root: {})
    monkeypatch.setattr(runtime, "stop_process", lambda process: setattr(process, "code", 0))
    request = {"session_id": "terminal", "environment_id": "local", "account": ACCOUNT_A, "path": str(tmp_path)}
    accounts = {"account-a": ACCOUNT_A, "account-b": ACCOUNT_B}
    item = runtime.ClaudeSupervisor(request, tmp_path, accounts, lambda account, args, prompt, live: [account, *args] + ([prompt] if prompt else []))
    item.start()
    return item, spawned


def opened(item, session, model="claude-sonnet-5-5"):
    """What Claude Code 2.1.296 reports when a session starts or resumes: SessionStart (no model), then the status line."""
    emit(item, "SessionStart", session_id=session)
    if model is not None:
        emit(item, "VibeStatus", session_id=session, model=model)


def emit(item, name, **values):
    runtime.write_claude_event(item._events, dict(values, hook_event_name=name, generation=str(item._generation)))


def test_equal_clock_ticks_preserve_subagent_start_before_stop(supervisor, monkeypatch):
    item, _ = supervisor
    opened(item, item._session)
    item.tick()
    emit(item, "UserPromptSubmit")
    item.tick()
    monkeypatch.setattr(time, "time_ns", lambda: 1234567890000000)
    emit(item, "SubagentStart", agent_id="still-running")
    emit(item, "Stop", background_tasks=0)
    assert len(list(item._events.glob("*.json"))) == 2
    assert not item.tick()["can_switch"]
    emit(item, "SubagentStop", agent_id="still-running")
    assert item.tick()["can_switch"]


def test_equal_clock_ticks_preserve_hook_and_status_events(tmp_path, monkeypatch):
    events = tmp_path / "events"
    monkeypatch.setattr(time, "time_ns", lambda: 1234567890000000)
    common = dict(VIBE_CLAUDE_CONTROL=str(events), VIBE_CLAUDE_GENERATION="1")
    hook(monkeypatch, "event", {"hook_event_name": "SubagentStart", "agent_id": "running"}, **common)
    hook(monkeypatch, "event", {"hook_event_name": "Stop", "background_tasks": []}, **common)
    hook(monkeypatch, "status", {"session_id": "session", "model": {"id": "example-model"}}, **common)
    recorded = [json.loads(path.read_text(encoding="utf-8"))["hook_event_name"]
                for path in sorted(events.glob("*.json"))]
    assert recorded == ["SubagentStart", "Stop", "VibeStatus"]


def test_event_sequence_is_shared_by_concurrent_hook_processes(tmp_path):
    events = tmp_path / "events"
    code = """import sys, time
from pathlib import Path
from vibe_launcher import runtime
time.time_ns = lambda: 1234567890000000
for index in range(5):
    runtime.write_claude_event(Path(sys.argv[1]), {"writer": sys.argv[2], "index": index})
"""
    children = []
    try:
        for writer in range(4):
            children.append(subprocess.Popen([sys.executable, "-c", code, str(events), str(writer)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        for child in children:
            _, errors = child.communicate(timeout=30)
            assert child.returncode == 0, errors.decode("utf-8", "replace")
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    paths = sorted(events.glob("*.json"))
    assert [int(path.stem) for path in paths] == list(range(1, 21))
    records = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    for writer in range(4):
        assert [event["index"] for event in records if event["writer"] == str(writer)] == list(range(5))


def test_invalid_event_sequence_blocks_new_work_without_overwriting(tmp_path, monkeypatch):
    events = tmp_path / "events"
    runtime.write_claude_event(events, {"hook_event_name": "Stop"})
    original = next(events.glob("*.json")).read_bytes()
    runtime.write_json(events.parent / "event-sequence.json", {"sequence": "invalid"})
    assert run_hook(monkeypatch, events, "UserPromptSubmit") == 2
    assert [path.read_bytes() for path in events.glob("*.json")] == [original]


def test_safe_point_needs_a_settled_turn_without_tools_or_subagents(supervisor):
    item, spawned = supervisor
    assert spawned[0][0][:2] == ["account-a", "--session-id"]
    assert not item.tick()["can_switch"]                      # nothing reported yet
    emit(item, "SessionStart", session_id=item._session, model="haiku", effort="low", permission_mode="default")
    assert item.tick()["can_switch"]
    emit(item, "UserPromptSubmit")
    emit(item, "PreToolUse", tool_use_id="t1")
    assert not item.tick()["can_switch"]
    emit(item, "PostToolUse", tool_use_id="t1")
    emit(item, "SubagentStart", agent_id="a1")
    emit(item, "Stop", background_tasks=0)
    assert not item.tick()["can_switch"]                      # a subagent still runs
    emit(item, "SubagentStop", agent_id="a1")
    emit(item, "Stop", background_tasks=1)
    assert not item.tick()["can_switch"]                      # a background task still runs
    emit(item, "UserPromptSubmit")
    emit(item, "Stop", background_tasks=0)
    snapshot = item.tick()
    assert snapshot["can_switch"] and snapshot["live"] == {"model": "haiku", "effort": "low", "permission_mode": "default"}


def test_switch_ends_the_cli_moves_the_transcript_and_resumes_the_same_session(supervisor, tmp_path):
    item, spawned = supervisor
    session = item._session
    runtime._write_private(runtime.profile_dir(tmp_path, ACCOUNT_A) / "projects" / "C--work" / (session + ".jsonl"), b"{}\n")
    opened(item, session)
    emit(item, "UserPromptSubmit")
    emit(item, "StopFailure", error="rate_limit")
    item.tick()
    item.request_switch("account-b")
    snapshot = item.tick()
    # Started, but not yet the active account: it has to report the session first.
    assert snapshot["account"] == "account-a" and snapshot["state"] == "switching" and snapshot["pending_account"] == "account-b"
    assert spawned[-1][0] == ["account-b", "--resume", session] and spawned[-1][1] == "2"
    assert (runtime.profile_dir(tmp_path, ACCOUNT_B) / "projects" / "C--work" / (session + ".jsonl")).exists()
    assert not runtime.claude_handoff_lock(item._events).exists()
    emit(item, "SessionStart", session_id=session)
    assert item.tick()["account"] == "account-a"                 # the status line has not reported the model yet
    emit(item, "VibeStatus", session_id=session, model="claude-sonnet-5-5")
    snapshot = item.tick()
    assert snapshot["account"] == "account-b" and snapshot["thread_id"] == session and snapshot["pending_account"] is None
    assert snapshot["continuation_ready"] and snapshot["last_error_kind"] == "usageLimitExceeded"
    item.resume_work()
    assert spawned[-1][0] == ["account-b", "--resume", session, runtime.CLAUDE_CONTINUE]


def test_old_generation_events_and_user_exit(supervisor):
    item, _ = supervisor
    runtime.write_json(item._events / "00000000000000000001-1.json", {"hook_event_name": "UserPromptSubmit", "generation": "0"})
    emit(item, "SessionStart", session_id=item._session)
    assert item.tick()["can_switch"]                          # the stale prompt from another run was ignored
    item._process.code = 0
    assert item.finished() == 0


def test_account_differences_are_offered_to_the_user(supervisor, tmp_path, monkeypatch):
    item, _ = supervisor
    request = {"session_id": "terminal", "environment_id": "local", "account": ACCOUNT_A, "path": str(tmp_path / "work")}
    controller = runtime.ManagedClaudeSession(request, tmp_path, item)
    controller.accounts.update({"account-b": ACCOUNT_B})
    monkeypatch.setattr(runtime, "check_login", lambda *args: True)
    monkeypatch.setattr(runtime, "sync_user_data", lambda *args, **kwargs: {})
    for account, plugins in ((ACCOUNT_A, {"review@m": True}), (ACCOUNT_B, {})):
        runtime._write_private(runtime.profile_dir(tmp_path, account) / "settings.json",
                               json.dumps({"enabledPlugins": plugins, "theme": "dark"}).encode())
    with pytest.raises(runtime.CodexHandoffBlocked) as blocked:
        controller.validate_target("account-b", {})
    assert blocked.value.gaps == [{"kind": "plugin", "name": "review"}]
    controller._allow_gaps.add("account-b")
    controller.validate_target("account-b", {})
    runtime._write_private(runtime.profile_dir(tmp_path, ACCOUNT_B) / "settings.json",
                           json.dumps({"enabledPlugins": {}, "theme": "light"}).encode())
    with pytest.raises(runtime.CodexControlError, match="settings"):
        controller.validate_target("account-b", {})
    # A default model only one profile sets is overridden by the conversation's own model at the restart.
    runtime._write_private(runtime.profile_dir(tmp_path, ACCOUNT_B) / "settings.json",
                           json.dumps({"enabledPlugins": {}, "theme": "dark", "model": "opus"}).encode())
    with pytest.raises(runtime.CodexControlError, match="settings"):
        controller.validate_target("account-b", {})
    controller.validate_target("account-b", {"model": "claude-haiku-5-5"})
    controller._closed.set()


def test_a_pool_holds_one_cli(tmp_path):
    with pytest.raises(ValueError):
        runtime.validate_control_accounts([ACCOUNT_A, dict(ACCOUNT_B, tool="codex")], "local")
    assert runtime.validate_control_accounts([ACCOUNT_A, ACCOUNT_B], "local") == {"account-a", "account-b"}


def test_simulated_quota_failure_marks_a_settled_turn(supervisor):
    item, _ = supervisor
    assert not item.simulate_quota_failure()                  # nothing settled yet
    emit(item, "SessionStart", session_id=item._session)
    emit(item, "UserPromptSubmit")
    emit(item, "Stop", background_tasks=0)
    item.tick()
    assert item.simulate_quota_failure() and item.snapshot()["last_error_kind"] == "usageLimitExceeded"


def test_account_synced_and_cli_internal_skills_are_neither_shared_nor_compared(tmp_path):
    first, second = ({"id": name, "tool": "claude", "auth": "subscription"} for name in ("one", "two"))
    for account, synced in ((first, "org-a_skill"), (second, "org-b_skill")):
        profile = runtime.profile_dir(tmp_path, account)
        (profile / "skills" / "synced" / synced).mkdir(parents=True)
        (profile / "skills" / "synced" / synced / "SKILL.md").write_text(synced, encoding="utf-8")
        (profile / "skills" / ".trash" / synced).mkdir(parents=True)                 # Claude's removed-skill bin
        (profile / "skills" / ".trash" / synced / "SKILL.md").write_text(synced, encoding="utf-8")
        (profile / "skills" / "mine").mkdir(parents=True)
        (profile / "skills" / "mine" / "SKILL.md").write_text("shared", encoding="utf-8")
    assert runtime.claude_profile_fingerprint(first, tmp_path) == runtime.claude_profile_fingerprint(second, tmp_path)
    files = runtime._shareable_files(runtime.profile_dir(tmp_path, first), "claude")
    assert files == [runtime.Path("skills/mine/SKILL.md")]
    (runtime.profile_dir(tmp_path, second) / "skills" / "mine" / "SKILL.md").write_text("changed", encoding="utf-8")
    assert runtime.claude_profile_fingerprint(first, tmp_path) != runtime.claude_profile_fingerprint(second, tmp_path)


def test_a_handoff_target_skips_the_first_run_wizard_and_keeps_the_folder_trusted(tmp_path):
    source, target = ({"id": name, "tool": "claude", "auth": "subscription"} for name in ("from", "to"))
    project = "/home/user/projects/hello"
    runtime.write_json(runtime.profile_dir(tmp_path, source) / ".claude.json", {
        "hasCompletedOnboarding": True, "projects": {project: {"hasTrustDialogAccepted": True}}})
    runtime.write_json(runtime.profile_dir(tmp_path, target) / ".claude.json", {
        "oauthAccount": {"emailAddress": "user@example.com"}, "projects": {"/elsewhere": {"hasTrustDialogAccepted": False}}})
    runtime.carry_claude_onboarding(source, target, tmp_path, project)
    data = runtime.json.loads(runtime.read_private(runtime.profile_dir(tmp_path, target) / ".claude.json"))
    assert data["hasCompletedOnboarding"] is True
    assert data["projects"][project] == {"hasTrustDialogAccepted": True}
    assert data["projects"]["/elsewhere"] == {"hasTrustDialogAccepted": False}       # other folders untouched
    assert data["oauthAccount"] == {"emailAddress": "user@example.com"}


def test_an_untrusted_folder_is_not_trusted_for_the_target(tmp_path):
    source, target = ({"id": name, "tool": "claude", "auth": "subscription"} for name in ("from", "to"))
    runtime.write_json(runtime.profile_dir(tmp_path, source) / ".claude.json", {"projects": {}})
    runtime.write_json(runtime.profile_dir(tmp_path, target) / ".claude.json", {"oauthAccount": {"x": 1}})
    runtime.carry_claude_onboarding(source, target, tmp_path, "/w")
    data = runtime.json.loads(runtime.read_private(runtime.profile_dir(tmp_path, target) / ".claude.json"))
    assert "projects" not in data and data["hasCompletedOnboarding"] is True


def test_one_time_notices_the_person_saw_are_not_shown_again_on_the_target(tmp_path):
    source, target = ({"id": name, "tool": "claude", "auth": "subscription"} for name in ("from", "to"))
    runtime.write_json(runtime.profile_dir(tmp_path, source) / ".claude.json", {
        "hasSeenAutoModeEntryWarning": True, "hasSeenAutoDefaultNotice": True, "lastReleaseNotesSeen": "2.1.296",
        "hasSeenSomethingElse": False, "oauthAccount": {"emailAddress": "source@example.com"}, "userID": "source"})
    runtime.write_json(runtime.profile_dir(tmp_path, target) / ".claude.json", {
        "oauthAccount": {"emailAddress": "target@example.com"}, "hasCompletedOnboarding": True, "hasSeenAutoDefaultNotice": True})
    runtime.carry_claude_onboarding(source, target, tmp_path, "/w")
    data = runtime.json.loads(runtime.read_private(runtime.profile_dir(tmp_path, target) / ".claude.json"))
    assert data["hasSeenAutoModeEntryWarning"] is True and data["lastReleaseNotesSeen"] == "2.1.296"
    assert "hasSeenSomethingElse" not in data                                   # only what was really seen
    assert data["oauthAccount"] == {"emailAddress": "target@example.com"} and "userID" not in data   # identity stays


def test_a_switch_before_anything_was_said_starts_the_conversation_fresh_on_the_target(supervisor, tmp_path):
    item, spawned = supervisor
    session = item._session
    opened(item, session)
    assert item.tick()["can_switch"]
    item.request_switch("account-b")                    # no transcript yet: --resume would find no conversation
    item.tick()
    assert spawned[-1][0] == ["account-b", "--session-id", session]
    opened(item, session)
    snapshot = item.tick()
    assert snapshot["account"] == "account-b" and snapshot["thread_id"] == session
    emit(item, "UserPromptSubmit")
    emit(item, "Stop", background_tasks=0)
    item.tick()
    item.request_switch("account-a")                     # once something was said, later starts resume it
    runtime._write_private(runtime.profile_dir(tmp_path, ACCOUNT_B) / "projects" / "C--work" / (session + ".jsonl"), b"{}\n")
    item.tick()
    assert spawned[-1][0] == ["account-a", "--resume", session]


# --- synchronous gate: a prompt or tool is either known to the handoff or refused -------------------------------------
def run_hook(monkeypatch, events, name, generation="1", **values):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(dict(values, hook_event_name=name)).encode())))
    monkeypatch.setenv("VIBE_CLAUDE_CONTROL", str(events))
    monkeypatch.setenv("VIBE_CLAUDE_GENERATION", generation)
    return runtime.claude_hook(["event", name])


def test_a_prompt_during_the_handoff_lock_is_refused_and_does_not_count(supervisor, monkeypatch, capsys):
    item, _ = supervisor
    emit(item, "SessionStart", session_id=item._session)
    item.tick()
    assert item._quiesce()                          # the handoff holds the lock
    assert run_hook(monkeypatch, item._events, "UserPromptSubmit", str(item._generation)) == 2
    assert run_hook(monkeypatch, item._events, "PreToolUse", str(item._generation), tool_use_id="t9") == 2
    assert "handoff" in capsys.readouterr().err
    assert run_hook(monkeypatch, item._events, "Stop", str(item._generation)) == 0     # ending work is never held
    item._release()
    item._read_events()
    assert item.snapshot()["can_switch"] and not item._tools
    assert run_hook(monkeypatch, item._events, "UserPromptSubmit", str(item._generation)) == 0    # lock released


def test_a_hook_that_cannot_record_blocks_only_work_starting_events(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    events = blocker / "events"                     # cannot be created: recording fails
    assert run_hook(monkeypatch, events, "UserPromptSubmit") == 2
    assert run_hook(monkeypatch, events, "PreToolUse", tool_use_id="t") == 2
    assert run_hook(monkeypatch, events, "Stop") == 0
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"not json")))
    assert runtime.claude_hook(["event", "PreToolUse"]) == 2          # unreadable input of a gated event


def test_a_lock_left_by_a_dead_supervisor_does_not_block(tmp_path, monkeypatch):
    events = tmp_path / "events"
    runtime.write_json(runtime.claude_handoff_lock(events), {"pid": 999999})
    monkeypatch.setattr(runtime, "process_alive", lambda pid: False)
    assert run_hook(monkeypatch, events, "UserPromptSubmit") == 0


def test_work_that_starts_while_the_target_is_checked_keeps_the_source(supervisor):
    item, spawned = supervisor
    opened(item, item._session)
    item.tick()
    item._validate_target = lambda target, live: emit(item, "UserPromptSubmit")      # a prompt slips in meanwhile
    item.request_switch("account-b")
    snapshot = item.tick()
    assert len(spawned) == 1 and item._process.code is None          # the source CLI was never stopped
    assert snapshot["account"] == "account-a" and snapshot["pending_account"] == "account-b"
    assert snapshot["state"] == "running" and not runtime.claude_handoff_lock(item._events).exists()


def test_a_late_tool_event_found_under_the_lock_keeps_the_source(supervisor):
    item, spawned = supervisor
    emit(item, "SessionStart", session_id=item._session)
    item.tick()
    emit(item, "PreToolUse", tool_use_id="late")        # recorded before the lock, not yet read
    item.request_switch("account-b")
    item._safe = lambda: True                            # the controller still believed the session idle
    item.tick()
    assert len(spawned) == 1 and item._process.code is None and item._account == "account-a"


def failing_target(supervisor, monkeypatch, code=None, raises=False):
    item, spawned = supervisor
    original = runtime.subprocess.Popen

    def popen(argv, **kwargs):
        if argv[0] == "account-b" and raises:
            raise OSError("cannot start")
        process = original(argv, **kwargs)
        if argv[0] == "account-b":
            process.code = code
        return process
    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    opened(item, item._session)
    item.tick()
    assert item._live["model"] == "claude-sonnet-5-5"                   # the source's model, learned from its status line
    item.request_switch("account-b")
    item.tick()
    return item, spawned


@pytest.mark.parametrize("code, raises", [(1, False), (None, True)])
def test_a_target_that_fails_to_start_brings_back_the_source(supervisor, monkeypatch, code, raises):
    item, spawned = failing_target(supervisor, monkeypatch, code, raises)
    snapshot = item.tick()
    assert spawned[-1][0][0] == "account-a" and snapshot["account"] == "account-a"
    assert snapshot["pending_account"] is None and "original account" in snapshot["reason"]
    assert item._process.code is None                    # only the restarted source runs
    emit(item, "SessionStart", session_id=item._session)
    assert item.tick()["state"] == "idle"


def test_a_target_that_never_reports_the_session_is_replaced_by_the_source(supervisor, monkeypatch):
    item, spawned = failing_target(supervisor, monkeypatch)
    target = item._process
    assert item.tick()["account"] == "account-a"             # still waiting for the target
    item._verifying["until"] = 0
    snapshot = item.tick()
    assert target.code == 0 and spawned[-1][0][0] == "account-a" and snapshot["account"] == "account-a"


@pytest.mark.parametrize("reported, committed", [("claude-opus-5-5", False), ("claude-sonnet-5-5", True), (None, False),
                                                 ("claude-sonnet-5", False), ("sonnet", False), ("Claude-Sonnet-5-5", False)])
def test_a_target_with_another_or_no_model_is_not_committed(supervisor, monkeypatch, reported, committed):
    """Exactly the source's full model id: a near id ("claude-sonnet-5"), an alias or a missing model is not verified."""
    item, spawned = failing_target(supervisor, monkeypatch)            # the source ran "sonnet"
    opened(item, item._session, reported)
    if reported is None:
        item._verifying["until"] = 0                         # no status line report within the limit
    snapshot = item.tick()
    assert (snapshot["account"] == "account-b") is committed and spawned[-1][0][0] == ("account-b" if committed else "account-a")


def test_when_even_the_source_cannot_start_the_checkpoint_needs_the_user(supervisor, monkeypatch):
    item, spawned = failing_target(supervisor, monkeypatch, code=1)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("gone")))
    snapshot = item.tick()
    assert snapshot["state"] == "needs_attention" and snapshot["account"] == "account-a" and not item._ours
    assert item.finished() == 1                          # the worker ends instead of waiting forever


@pytest.mark.parametrize("version, supported", [(b"2.1.295 (Claude Code)", True), (b"2.1.294 (Claude Code)", False),
                                                (b"unknown", False)])
def test_safe_management_needs_a_claude_code_that_blocks_failed_hooks(tmp_path, monkeypatch, version, supported):
    def run(argv, **kwargs):
        output = b"--session-id --settings --resume" if argv[-1] == "--help" else version
        return runtime.subprocess.CompletedProcess(argv, 0, output)
    monkeypatch.setattr(runtime.subprocess, "run", run)
    assert REAL_SUPPORTED("claude", {}, tmp_path) is supported


@pytest.mark.parametrize("reported", ["another-session", None])
def test_a_target_that_opened_another_conversation_is_rolled_back(supervisor, monkeypatch, reported):
    """Regression: a target reporting a different (or no) session id was accepted as a successful switch."""
    item, spawned = failing_target(supervisor, monkeypatch)
    expected = item._session
    emit(item, "SessionStart", **({"session_id": reported} if reported else {}))
    emit(item, "VibeStatus", session_id=reported or "x", model="claude-sonnet-5-5")
    snapshot = item.tick()
    assert snapshot["account"] == "account-a" and snapshot["pending_account"] is None
    assert spawned[-1][0][0] == "account-a" and snapshot["thread_id"] == expected
    assert "original account" in snapshot["reason"]


def test_a_backlog_beyond_one_batch_postpones_the_handoff(supervisor, monkeypatch):
    """Regression: with more events queued than one read takes, a PreToolUse left unread let the handoff stop the CLI."""
    item, spawned = supervisor
    opened(item, item._session)
    item.tick()
    monkeypatch.setattr(runtime, "CLAUDE_EVENT_BATCH", 3)
    for index in range(6):                       # one batch in tick, one under the lock, the tool still unread
        runtime.write_json(item._events / ("%020d-1.json" % index), {"hook_event_name": "PostModelSwitch",
                                                                       "generation": str(item._generation)})
    runtime.write_json(item._events / ("%020d-1.json" % 9), {"hook_event_name": "PreToolUse", "tool_use_id": "late",
                                                               "generation": str(item._generation)})
    item._safe = lambda: True                    # the controller had not yet seen the backlog
    item.request_switch("account-b")
    snapshot = item.tick()
    assert len(spawned) == 1 and item._process.code is None and snapshot["account"] == "account-a"
    assert not runtime.claude_handoff_lock(item._events).exists() and snapshot["pending_account"] == "account-b"
    del item._safe
    item.tick()
    item.tick()                                  # the rest of the queue is read: a tool is running, still no switch
    assert "late" in item._tools and len(spawned) == 1


def test_the_status_line_records_session_and_model_once_per_change(tmp_path, monkeypatch):
    events = tmp_path / "control" / "events"
    data = {"session_id": "s1", "model": {"id": "claude-sonnet-5-5", "display_name": "Sonnet"}}
    for _ in range(2):
        hook(monkeypatch, "status", data, VIBE_CLAUDE_CONTROL=str(events), VIBE_CLAUDE_GENERATION="2")
    hook(monkeypatch, "status", dict(data, model={"id": "claude-opus-5-5"}), VIBE_CLAUDE_CONTROL=str(events),
         VIBE_CLAUDE_GENERATION="2")
    recorded = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(events.glob("*.json"))]
    assert recorded == [{"session_id": "s1", "model": "claude-sonnet-5-5", "hook_event_name": "VibeStatus", "generation": "2"},
                        {"session_id": "s1", "model": "claude-opus-5-5", "hook_event_name": "VibeStatus", "generation": "2"}]


def test_a_target_whose_status_line_reports_another_session_is_rolled_back(supervisor, monkeypatch):
    item, spawned = failing_target(supervisor, monkeypatch)
    emit(item, "SessionStart", session_id=item._session)
    emit(item, "VibeStatus", session_id="another-session", model="claude-sonnet-5-5")
    snapshot = item.tick()
    assert snapshot["account"] == "account-a" and spawned[-1][0][0] == "account-a"


def test_a_source_whose_model_is_not_proven_keeps_its_account(supervisor, tmp_path):
    """Regression: the source (account default Haiku) had not reported its model yet; the switch used the target's
    default (Opus) and committed. Without the source's own model report nothing is stopped and the user is told."""
    item, spawned = supervisor
    item._accounts = {"account-a": dict(ACCOUNT_A, model="claude-haiku-5-5"), "account-b": dict(ACCOUNT_B, model="claude-opus-5-5")}
    emit(item, "SessionStart", session_id=item._session)          # no model, no status line yet
    item.tick()
    item.request_switch("account-b")
    snapshot = item.tick()
    assert len(spawned) == 1 and item._process.code is None and snapshot["account"] == "account-a"
    assert snapshot["pending_account"] == "account-b" and "current model" in snapshot["reason"]
    emit(item, "VibeStatus", session_id=item._session, model="claude-haiku-5-5")
    item.tick()                                                   # proven now: the target starts on the source's model
    argv = spawned[-1][0]
    assert argv[0] == "account-b" and item._verifying["expected_model"] == "claude-haiku-5-5"
    emit(item, "SessionStart", session_id=item._session)
    emit(item, "VibeStatus", session_id=item._session, model="claude-opus-5-5")    # the target's own default
    assert item.tick()["account"] == "account-a" and spawned[-1][0][0] == "account-a"


def test_a_model_switch_needs_a_new_report_before_handing_off(supervisor):
    item, spawned = supervisor
    opened(item, item._session)
    emit(item, "PostModelSwitch", model="opus")                   # an alias: not the evidence itself
    item.tick()
    item.request_switch("account-b")
    item.tick()
    assert len(spawned) == 1 and item.snapshot()["pending_account"] == "account-b"
    emit(item, "VibeStatus", session_id=item._session, model="claude-opus-5-5")
    item.tick()
    assert spawned[-1][0][0] == "account-b" and item._verifying["expected_model"] == "claude-opus-5-5"


def recording_commands(item):
    started = []
    item._command_for = lambda account, args, prompt, live: started.append((account, dict(live))) or [account, *args]
    return started


@pytest.mark.parametrize("target_starts", [True, False])
def test_settings_changed_during_the_target_check_are_confirmed_before_stopping(supervisor, monkeypatch, target_starts):
    """Regression: the user switched Sonnet -> Opus while the target was checked; the target started on Opus but was
    verified against the stale Sonnet, failed, and the rollback restarted Sonnet, undoing the user's choice."""
    item, spawned = supervisor
    started = recording_commands(item)
    opened(item, item._session, "claude-sonnet-5-5")
    item.tick()

    def user_switches_model(target, live):
        assert live["model"] == "claude-sonnet-5-5"
        emit(item, "PostModelSwitch", model="opus")
        emit(item, "VibeStatus", session_id=item._session, model="claude-opus-5-5")
    item._validate_target = user_switches_model
    item.request_switch("account-b")
    snapshot = item.tick()
    # Read under the lock: the snapshot changed, so the source keeps running and the lock is released.
    assert not started and item._process.code is None and snapshot["account"] == "account-a"
    assert snapshot["pending_account"] == "account-b" and not runtime.claude_handoff_lock(item._events).exists()
    checked = []
    item._validate_target = lambda target, live: checked.append(live["model"])
    if not target_starts:
        original = item._command_for
        item._command_for = lambda account, args, prompt, live: (_ for _ in ()).throw(OSError("no")) if account == "account-b" \
            else original(account, args, prompt, live)
    item.tick()                                          # checked again with the new settings, then handed off
    assert checked == ["claude-opus-5-5"]
    if target_starts:
        assert started == [("account-b", started[0][1])] and started[0][1]["model"] == "claude-opus-5-5"
        assert item._verifying["expected_model"] == "claude-opus-5-5"
        opened(item, item._session, "claude-opus-5-5")
        assert item.tick()["account"] == "account-b"
    else:                                                # the rollback keeps the user's Opus, never the stale Sonnet
        assert started[-1][0] == "account-a" and started[-1][1]["model"] == "claude-opus-5-5"
        assert item.snapshot()["account"] == "account-a"
