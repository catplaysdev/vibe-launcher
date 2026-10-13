from __future__ import annotations

import base64
import json
import re
import socket
import struct
import subprocess
import sys
import time

import pytest

from vibe_launcher import runtime
from vibe_launcher.codex_control import CodexControlError, ManagedCodexBridge


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


def test_two_owners_and_bad_auth_rejected(managed):
    _bridge, endpoint, capability, _ = managed
    bad, header = _connect(endpoint, "wrong-secret")
    assert b"403" in header
    bad.close()
    conn, header = _connect(endpoint, capability)
    assert b"101" in header
    second, header = _connect(endpoint, capability)
    assert b"409" in header
    second.close()
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
