"""The SSH helpers: each feature ships what its own path reaches, and every command fits the Windows limit."""
import ast
import base64
import hashlib
import json
import lzma
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from vibe_launcher import i18n, runtime, service

SOURCE = Path(runtime.__file__).read_bytes()
TREE = ast.parse(SOURCE)
DEFINED = {node.name: node for node in TREE.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
CODEX = {"id": "example", "name": "Example", "tool": "codex", "auth": "subscription"}

# Entry points of each helper. ``perform`` only dispatches, so its branches are named here instead of followed.
ROOTS = {
    "cli": {"main", "validate_request", "doctor", "authenticate", "run_worker", "session_outcome", "check_settings",
            "ensure_project_dir", "direct_outcome", "list_sessions", "session_action", "manage_tools", "browse_listing",
            "fetch_quota", "quota_file", "manual_import", "sync_project_history", "operation_result", "augmented_path"},
    "managed": {"main", "validate_request", "doctor", "authenticate", "run_worker", "session_outcome", "check_settings",
                "ensure_project_dir", "direct_outcome", "augmented_path", "run_managed_codex", "ManagedCodexSession",
                "ManagedCodexBridge", "CodexQuotaMonitor"},
    "control": {"main", "validate_request", "codex_control_action", "operation_result", "augmented_path"},
}


def reachable(roots):
    seen, todo = set(), list(roots)
    while todo:
        name = todo.pop()
        if name in seen or name not in DEFINED:
            continue
        seen.add(name)
        if name != "perform":
            node = DEFINED[name]
            todo += [item.id for item in ast.walk(node) if isinstance(item, ast.Name)]
            todo += [item.attr for item in ast.walk(node) if isinstance(item, ast.Attribute)]
    return seen


def guarded(name, feature):
    # Reached only behind ``if WINDOWS`` (an SSH host is never Windows), or, in the plain helper, only behind a Codex
    # subscription launch or a control request, which ``remote_feature`` sends to the managed / control helpers.
    # The Claude hook branch of main() only runs from a managed helper's own --claude-hook command.
    return (name in service.WINDOWS_ONLY or (feature == "cli" and name in service.MANAGED_ONLY | service.QUOTA_FLOW)
            or (feature == "control" and name in service.MANAGED_ONLY))


@pytest.mark.parametrize("feature", sorted(ROOTS))
def test_every_definition_a_feature_reaches_is_shipped(feature):
    missing = sorted(name for name in reachable(ROOTS[feature])
                     if not guarded(name, feature) and not service._shipped(name, feature))
    assert not missing, f"{feature} helper would raise NameError for {missing}"


def shipped_namespace(feature, monkeypatch, locale="en"):
    import types
    _, compressed = service._compressed_runtime(SOURCE, feature, locale)
    module = types.ModuleType("vibe_proof_" + feature)      # as the bootstrap does: dataclasses look the module up
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(lzma.decompress(base64.b85decode(compressed)), "runtime-proof.py", "exec"), module.__dict__)
    for name in service.WINDOWS_ONLY:       # the tests themselves run on Windows; a host never needs these
        module.__dict__[name] = getattr(runtime, name)
    return module.__dict__


def test_managed_helper_publishes_ordered_claude_events(tmp_path, monkeypatch):
    namespace = shipped_namespace("managed", monkeypatch)
    events = tmp_path / "events"
    namespace["write_claude_event"](events, {"hook_event_name": "SubagentStart"})
    namespace["write_claude_event"](events, {"hook_event_name": "Stop"})
    recorded = [json.loads(path.read_text(encoding="utf-8"))["hook_event_name"]
                for path in sorted(events.glob("*.json"))]
    assert recorded == ["SubagentStart", "Stop"]


@pytest.mark.parametrize("locale", i18n.LANGUAGES)
@pytest.mark.parametrize("request_", [
    {"action": "launch", "account": CODEX, "path": "/home/user/" + "example/" * 40},
    {"action": "launch", "account": dict(CODEX, tool="claude"), "path": "/home/user/example"},
    {"action": "codex-control", "session_id": "session", "control_action": "policy", "environment_id": "ssh",
     "accounts": [dict(CODEX, id=f"account{i}", environment_id="ssh") for i in range(8)],
     "policy": {"id": "flow", "name": "Flow", "environment_id": "ssh", "account_ids": [f"account{i}" for i in range(8)]}},
    {"action": "quota", "account": CODEX},
])
def test_every_helper_fits_the_windows_command_line(locale, request_):
    request = dict(request_, language=locale)
    command = service.remote_command(request)
    assert len(command) <= 28000 and len(command.encode("utf-8")) <= 120000


def test_launch_with_a_saved_flow_fits_and_keeps_only_nonsecret_metadata():
    pool = [dict(CODEX, id=f"account{i}") for i in range(16)]
    request = {"action": "launch", "account": CODEX, "path": "/home/user/example", "language": "zh-TW",
               "resume_thread_id": "thread-0123", "quota_accounts": pool,
               "quota_policy": {"id": "flow", "name": "Flow", "environment_id": "local",
                                "account_ids": [item["id"] for item in pool]}}
    assert len(service.remote_command(request)) <= 28000
    with pytest.raises(ValueError):
        service.remote_command(dict(request, quota_accounts=[dict(pool[0], credential_revision="x")] + pool[1:]))


def test_compact_transport_round_trips_through_a_real_command_line():
    """Windows hands the argument to Python as UTF-16; the host side decodes the exact bytes (os.fsencode)."""
    request = {"action": "launch", "account": CODEX, "path": "/home/user/example"}
    args = shlex.split(service.remote_command(request))
    assert len(args) == 5 and args[:3] == ["/usr/bin/python3", "-I", "-c"]
    digest = next(node.value for node in ast.walk(ast.parse(args[3]))
                  if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) == 64)
    loader = args[3].split("sys.exit(78)")[0] + "sys.stdout.write(hashlib.sha256(b).hexdigest())"
    result = subprocess.run([sys.executable, "-I", "-c", loader, args[4]], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout == digest == service._compressed_runtime(SOURCE, "managed", "en")[0]
    # Every character is a printable BMP ideograph: one UTF-16 unit on Windows, no shell quoting needed.
    assert all(0x4E00 <= ord(char) < 0x4E00 + 2 ** 14 for char in args[4])


def test_small_helpers_keep_the_ascii_transport():
    for request in ({"action": "quota", "account": CODEX},
                    {"action": "codex-control", "session_id": "s", "control_action": "status", "accounts": []}):
        args = shlex.split(service.remote_command(request))
        assert len(args) == 4
        assert args[3].isascii()


def test_control_helper_answers_from_the_shipped_module(tmp_path, monkeypatch):
    namespace = shipped_namespace("control", monkeypatch)
    request = {"action": "codex-control", "nonce": "n", "operation_id": "op", "session_id": "session",
               "control_action": "status", "environment_id": "local", "accounts": []}
    assert namespace["perform"](request, tmp_path) == 0
    result = json.loads((tmp_path / "results" / "op.json").read_text(encoding="utf-8"))["result"]
    assert result["status"] == "unmanaged"
    assert "ManagedCodexBridge" not in namespace and "sync_user_data" not in namespace


def test_managed_helper_runs_its_controller_from_the_shipped_module(tmp_path, monkeypatch):
    namespace = shipped_namespace("managed", monkeypatch)

    def offline(*_args):
        raise RuntimeError("offline test")
    namespace["fetch_quota"] = offline           # the monitor must not start a real Codex

    class Bridge:
        def snapshot(self):
            return dict(state="idle", reason="", account="example", thread_id="thread", can_switch=True,
                        pending_account=None, last_error_kind=None, rate_limits=None, rate_limits_checked_at=None)
        tick = snapshot

        def close(self):
            pass

    request = {"session_id": "session", "environment_id": "local", "account": CODEX, "path": str(tmp_path),
               "quota_accounts": [CODEX], "quota_policy": {"id": "flow", "name": "Flow", "environment_id": "local",
                                                          "account_ids": ["example"]}}
    controller = namespace["ManagedCodexSession"](request, tmp_path, Bridge())
    try:
        assert controller.tick()["policy"]["id"] == "flow"
    finally:
        controller.close()
    assert "manage_tools" not in namespace and "codex_control_action" not in namespace
