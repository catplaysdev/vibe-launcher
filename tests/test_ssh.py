"""SSH: the helpers sent to hosts, folder browsing, connection cancel, passwords and host security.

- remote helpers: The SSH helpers: each feature ships what its own path reaches, and every command fits the Windows limit.
- remote browse: Picking a project folder on an SSH host: the listing action, the client that fetches it, and the picker dialog.
- connection cancel
- ssh password
- security
"""
import ast
import base64
import hashlib
import io
import json
import lzma
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QComboBox
from vibe_launcher import askpass, i18n, launch_dialogs, remote_browse, runner, runtime, service, ssh_security, ui
from vibe_launcher.launch_dialogs import RemoteFolderDialog
from vibe_launcher.remote_browse import RemoteBrowseError
from vibe_launcher.service import ssh_args
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow


# --- remote helpers ---------------------------------------------------------------------------------------------------
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


@pytest.mark.parametrize("locale", i18n.LANGUAGES)
def test_launch_with_a_saved_flow_fits_and_keeps_only_nonsecret_metadata(locale):
    pool = [dict(CODEX, id=f"account{i}") for i in range(16)]
    ids = [item["id"] for item in pool]
    request = {"action": "launch", "account": CODEX, "path": "/home/user/example", "language": locale,
               "resume_thread_id": "thread-0123", "quota_accounts": pool,
               "quota_policy": {"id": "flow", "name": "Flow", "environment_id": "local", "account_ids": ids,
                                "flow": runtime.quota_template("priority", ids)}}
    assert len(service.remote_command(request)) <= 28000
    with pytest.raises(ValueError):
        service.remote_command(dict(request, quota_accounts=[dict(pool[0], credential_revision="x")] + pool[1:]))


def test_compact_transport_round_trips_through_a_real_command_line():
    """Windows hands the argument to Python as UTF-16; the host side decodes the exact bytes (os.fsencode)."""
    pool = [dict(CODEX, id=f"account{i}") for i in range(4)]
    request = {"action": "launch", "account": CODEX, "path": "/home/user/中文 ' ; $(bad)", "quota_accounts": pool,
               "quota_policy": {"id": "flow", "name": "Flow", "environment_id": "local",
                                "account_ids": [item["id"] for item in pool]}}
    args = shlex.split(service.remote_command(request))
    assert len(args) == 5 and args[:3] == ["/usr/bin/python3", "-I", "-c"]
    assert "$(bad)" not in args[3] and "account1" not in args[3]       # the request travels compressed with the helper
    digest = next(node.value for node in ast.walk(ast.parse(args[3]))
                  if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) == 64)
    loader = args[3].split("sys.exit(78)")[0] + "sys.stdout.write(hashlib.sha256(b).hexdigest()+' '+q)"
    result = subprocess.run([sys.executable, "-I", "-c", loader, args[4]], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    received, payload = result.stdout.split(" ")
    assert received == digest == service._compressed_runtime(SOURCE, "managed", "en")[0]
    assert json.loads(base64.urlsafe_b64decode(payload)) == request     # what the helper's main() decodes
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


# --- remote browse ----------------------------------------------------------------------------------------------------
# ------------------------------------------------------------------------------ what runs on the host
def test_the_listing_shows_only_folders_sorted_and_marks_the_parent(tmp_path):
    for name in ("beta", "Alpha", ".hidden"):
        (tmp_path / name).mkdir()
    (tmp_path / "file.txt").write_text("x")
    listing = runtime.browse_listing(str(tmp_path))
    assert listing["dirs"] == [".hidden", "Alpha", "beta"] and listing["exists"] is True
    assert listing["path"] == os.path.normpath(str(tmp_path)) and listing["parent"] == os.path.dirname(listing["path"])


def test_a_path_that_is_not_there_shows_its_nearest_existing_parent(tmp_path):
    (tmp_path / "real").mkdir()
    listing = runtime.browse_listing(str(tmp_path / "real" / "nope" / "deeper"))
    assert listing["path"] == os.path.normpath(str(tmp_path / "real")) and listing["exists"] is False
    assert listing["requested"].endswith(os.path.join("nope", "deeper"))


def test_tilde_means_the_home_folder_and_a_huge_folder_is_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "BROWSE_LIMIT", 2)
    for name in "abc":
        (tmp_path / name).mkdir()
    listing = runtime.browse_listing(str(tmp_path))
    assert listing["dirs"] == ["a", "b"] and listing["truncated"] is True
    assert runtime.browse_listing("~")["path"] == os.path.normpath(os.path.expanduser("~"))


def test_the_root_has_no_parent():
    root = os.path.abspath(os.sep)
    assert runtime.browse_listing(root)["parent"] == ""


def test_the_browse_action_is_valid_and_a_bad_path_is_not():
    runtime.validate_request({"action": "browse", "nonce": "n", "environment_id": "gpu", "path": "~/work"})
    with pytest.raises(ValueError):
        runtime.validate_request({"action": "browse", "nonce": "n", "environment_id": "gpu", "path": "a\nb"})


# ------------------------------------------------------------------------------ the client
ENV = {"id": "gpu", "name": "GPU", "kind": "ssh", "ssh_target": "gpu-lab"}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """No real ssh: capture the request the client builds and answer through a fake ``run``."""
    seen = {}
    monkeypatch.setattr(remote_browse, "ssh_args", lambda target, request, **k: seen.update(request=request, target=target) or ["ssh", target])
    monkeypatch.setattr(remote_browse, "_auth_environment", lambda *a: None)
    monkeypatch.setattr(remote_browse.askpass, "cleanup", lambda env: None)
    return seen


def answer(seen, listing=None, stderr=b"", code=0):
    def run(args, **kwargs):
        out = b"noise\n"
        if listing is not None:
            out += ("VIBE_BROWSE_" + seen["request"]["nonce"] + ":" + json.dumps(listing) + "\n").encode()
        return subprocess.CompletedProcess(args, code, out, stderr)
    return run


def test_the_client_returns_the_hosts_listing(wired, tmp_path):
    listing = {"path": "/home/me", "dirs": ["app"], "parent": "/home", "exists": True, "home": "/home/me"}
    got = remote_browse.list_remote(ENV, "~", tmp_path, tmp_path / "db", run=answer(wired, listing))
    assert got == listing
    assert wired["request"]["action"] == "browse" and wired["request"]["path"] == "~" and wired["target"] == "gpu-lab"


def test_a_failed_connection_shows_the_last_line_of_sshs_complaint(wired, tmp_path):
    run = answer(wired, None, stderr=b"warning\nPermission denied (publickey).\n", code=255)
    with pytest.raises(RemoteBrowseError, match="Permission denied"):
        remote_browse.list_remote(ENV, "~", tmp_path, tmp_path / "db", run=run)


def test_garbage_from_the_host_is_not_trusted(wired, tmp_path):
    for bad in ({"path": "/x"}, {"dirs": "nope"}):
        with pytest.raises(RemoteBrowseError, match="無法解讀"):
            remote_browse.list_remote(ENV, "~", tmp_path, tmp_path / "db", run=answer(wired, bad))


def test_timeouts_and_a_local_environment_are_explained(wired, tmp_path):
    def slow(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 1)
    with pytest.raises(RemoteBrowseError, match="逾時"):
        remote_browse.list_remote(ENV, "~", tmp_path, tmp_path / "db", run=slow)
    with pytest.raises(RemoteBrowseError, match="不是 SSH"):
        remote_browse.list_remote({"id": "local", "kind": "local"}, "~", tmp_path, tmp_path / "db")


def test_a_cancelled_password_window_is_reported_as_cancelled(wired, tmp_path, monkeypatch):
    monkeypatch.setattr(remote_browse, "_auth_environment", lambda *a: {"X": "1"})
    monkeypatch.setattr(remote_browse.askpass, "was_cancelled", lambda env: True)
    with pytest.raises(RemoteBrowseError, match="已取消"):
        remote_browse.list_remote(ENV, "~", tmp_path, tmp_path / "db", run=answer(wired, None, code=255))


def test_an_environment_that_does_not_remember_passwords_still_gets_a_non_saving_prompt(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(remote_browse.askpass, "ssh_environment", lambda environment, root, db: asked.append(environment) or {"ok": 1})
    remote_browse._auth_environment({**ENV, "remember_seconds": 0, "ssh_auth_mode": "password"}, tmp_path, tmp_path / "db")
    remote_browse._auth_environment({**ENV, "remember_seconds": 3600, "ssh_auth_mode": "password"}, tmp_path, tmp_path / "db")
    assert asked[0]["ssh_auth_mode"] == "interactive" and asked[0]["remember_seconds"] > 0      # asked every time, never saved
    assert asked[1]["ssh_auth_mode"] == "password"                                              # the existing remember policy is kept


# ------------------------------------------------------------------------------ the picker
BROWSE_TREE = {
    "~": {"path": "/home/me", "exists": True, "home": "/home/me", "parent": "/home", "dirs": [".cache", "app", "docs"]},
    "/home/me": {"path": "/home/me", "exists": True, "home": "/home/me", "parent": "/home", "dirs": [".cache", "app", "docs"]},
    "/home/me/app": {"path": "/home/me/app", "exists": True, "home": "/home/me", "parent": "/home/me", "dirs": ["src"]},
    "/home": {"path": "/home", "exists": True, "home": "/home/me", "parent": "/", "dirs": ["me"]},
    "/home/me/new": {"path": "/home/me", "exists": False, "home": "/home/me", "parent": "/home", "dirs": ["app"]},
}


def lister(path):
    if path == "/boom":
        raise RemoteBrowseError("連線逾時。")
    return BROWSE_TREE[path]


def make_dialog(qtbot, start="~"):
    dialog = RemoteFolderDialog("GPU", start, lister)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog.choose.isEnabled() or "逾時" in dialog.status.text(), timeout=3000)
    return dialog


def names(dialog):
    return [dialog.list.item(i).text() for i in range(dialog.list.count())]


def test_the_picker_lists_folders_hides_dotted_ones_and_can_show_them(qtbot):
    dialog = make_dialog(qtbot)
    assert names(dialog) == ["app", "docs"] and dialog.path.text() == "/home/me"
    dialog.hidden.setChecked(True)
    assert names(dialog) == [".cache", "app", "docs"]


def test_double_clicking_goes_in_and_up_goes_back(qtbot):
    dialog = make_dialog(qtbot)
    dialog._enter(dialog.list.item(0))                                  # app
    qtbot.waitUntil(lambda: dialog.path.text() == "/home/me/app", timeout=3000)
    assert names(dialog) == ["src"]
    dialog.up.click()
    qtbot.waitUntil(lambda: dialog.path.text() == "/home/me", timeout=3000)


def test_choosing_returns_the_folder_being_shown(qtbot):
    dialog = make_dialog(qtbot)
    dialog._enter(dialog.list.item(0))
    qtbot.waitUntil(lambda: dialog.path.text() == "/home/me/app", timeout=3000)
    dialog.choose.click()
    assert dialog.chosen() == "/home/me/app" and dialog.result() == dialog.DialogCode.Accepted


def test_a_missing_path_falls_back_with_a_warning_and_can_still_be_chosen(qtbot):
    dialog = make_dialog(qtbot, "/home/me/new")
    assert "不存在" in dialog.status.text() and dialog.choose.isEnabled()


def test_an_error_is_shown_and_nothing_can_be_chosen(qtbot):
    dialog = RemoteFolderDialog("GPU", "/boom", lister)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: "逾時" in dialog.status.text(), timeout=3000)
    assert not dialog.choose.isEnabled()


def test_a_late_answer_for_an_older_request_is_ignored(qtbot):
    dialog = make_dialog(qtbot)
    dialog._ticket += 5
    dialog._on_loaded(1, {"path": "/stale", "dirs": []}, "")
    assert dialog.path.text() == "/home/me"


# ------------------------------------------------------------------------------ the path field in the project dialog
def test_the_browse_button_works_for_ssh_environments_and_fills_the_field(qtbot, tmp_path):
    dialog = ui.RecordDialog("t", [("environment_id", "env", ""), ("path", "path", "")], {"environment_id": "local"},
                             {"environment_id": [("本機", "local"), ("GPU", "gpu")]}, None)
    qtbot.addWidget(dialog)
    asked = []
    assist = launch_dialogs.PathAssist(dialog, "path", "environment_id", {"local": "local", "gpu": "ssh"}, {},
                                       lambda env, start: asked.append((env, start)) or "/home/me/app")
    combo: QComboBox = dialog.fields["environment_id"]
    combo.setCurrentIndex(combo.findData("gpu"))
    dialog.fields["path"].setText("~/ap")
    assert assist.browse.isEnabled() and "連線" in assist.browse.toolTip()
    assist.browse.click()
    assert asked == [("gpu", "~/ap")] and dialog.fields["path"].text() == "/home/me/app"
    asked.clear()
    assist.browse_remote = lambda env, start: ""
    assist.browse.click()
    assert dialog.fields["path"].text() == "/home/me/app"                 # cancelling changes nothing


def test_the_window_opens_the_picker_only_for_ssh_environments(qtbot, tmp_path, monkeypatch):
    from vibe_launcher.store import Store
    store = Store(tmp_path / "w.db")
    store.save("environments", {"id": "gpu", "name": "GPU", "kind": "ssh", "ssh_target": "gpu-lab"})

    class Svc:
        supports_spawn = False

        def start(self, *a, **k):
            pass
    window = ui.VibeLauncherWindow(store, Svc())
    qtbot.addWidget(window, before_close_func=lambda w: w.workspace.shutdown())
    assert window._browse_remote("local", "") == ""
    opened = []

    def fake_exec(self):
        opened.append((self.windowTitle(), self.path.text()))
        self._chosen = "/home/me/app"
        return ui.QDialog.DialogCode.Accepted
    monkeypatch.setattr(RemoteFolderDialog, "exec", fake_exec)
    monkeypatch.setattr(remote_browse, "list_remote", lambda *a, **k: BROWSE_TREE["~"])
    assert window._browse_remote("gpu", "~/x") == "/home/me/app"
    assert opened and "GPU" in opened[0][0]


# --- connection cancel ------------------------------------------------------------------------------------------------
def setup(tmp_path):
    store = Store(tmp_path / "state.db")
    account = store.save("accounts", {"id": "a", "name": "A", "tool": "codex", "auth": "subscription"})
    env = store.save("environments", {"id": "host", "name": "H", "kind": "ssh", "ssh_target": "user@server"})
    store.save("sessions", {"id": "s", "name": "S", "account_id": "a", "environment_id": "host", "path": "/tmp", "status": "unknown"})
    return store, env, {"action": "launch", "nonce": "n", "session_id": "s", "environment_id": "host", "account": account}


def test_config_alias_with_underscore_is_accepted(tmp_path):
    store = Store(tmp_path / "db")
    assert store.save("environments", {"name": "Lab", "kind": "ssh", "ssh_target": "lab-target_A"})["ssh_target"] == "lab-target_A"


def test_windows_fingerprint_prompt_is_not_a_password():
    prompt = "The authenticity of host 'example' can't be established.\nED25519 key fingerprint is SHA256:example.\nAre you sure you want to continue connecting (yes/no/[fingerprint])?"
    assert askpass.is_host_confirmation(prompt)
    assert not askpass.is_host_confirmation("Password:")
    assert not askpass.is_host_confirmation("OTP (yes/no)?")


def test_cancel_before_auth_is_removable_and_stop_success_is_ended(tmp_path, monkeypatch):
    store, env, request = setup(tmp_path)
    context = {"VIBE_SSH_ROOT": str(tmp_path), "VIBE_SSH_ATTEMPT": "attempt"}
    monkeypatch.setattr(askpass, "ssh_environment", lambda *a: context)
    monkeypatch.setattr(askpass, "cleanup", lambda *a: None)
    monkeypatch.setattr(runner, "ssh_args", lambda *a, **k: ["fake-ssh"])
    class Cancelled:
        stdout = io.BytesIO(b"")
        def wait(self):
            return 255
    askpass.mark_cancelled(context)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: Cancelled())
    runner.run_remote(store, request, env)
    assert store.get("sessions", "s")["status"] == "failed"
    store.delete("sessions", "s")


def test_successful_remote_stop_updates_gui_state(tmp_path, monkeypatch):
    store, env, request = setup(tmp_path)
    request["action"] = "stop"
    monkeypatch.setattr(askpass, "ssh_environment", lambda *a: None)
    monkeypatch.setattr(runner, "ssh_args", lambda *a, **k: ["fake-ssh"])
    class Stopped:
        stdout = io.BytesIO(b"")
        def wait(self):
            return 0
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: Stopped())
    runner.run_remote(store, request, env)
    assert store.get("sessions", "s")["status"] == "ended"


def test_unknown_record_can_be_forgotten_without_remote_stop(tmp_path, monkeypatch, qtbot):
    store, env, request = setup(tmp_path)
    view = MainWindow(store, object())
    qtbot.addWidget(view)
    view.set_sessions_grouping("time")         # one flat list: row 0 is the record
    view.sessions_list.setCurrentRow(0)
    monkeypatch.setattr(view, "_notice", lambda text: pytest.fail(text))
    messages = []
    def confirm(title, text, **kwargs):
        messages.append(text)
        assert kwargs.get("danger"), "removing an unknown session must use the destructive confirmation"
        return True
    monkeypatch.setattr(view, "_confirm", confirm)
    view.remove_session()
    assert store.list("sessions") == []
    assert "不會停止" in messages[0]


def test_identity_files_and_jump_are_inherited(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".ssh").mkdir()
    config = tmp_path / ".ssh/config"
    config.write_text("Host lab-target_A\n HostName 192.0.2.1\n IdentityFile ~/.ssh/existing-one\n IdentityFile ~/.ssh/existing-two\n ProxyJump lab-jump_A\nHost lab-jump_A\n HostName 192.0.2.2\n", encoding="utf-8")
    from vibe_launcher.runtime import protect_path
    protect_path(config)
    settings = ssh_security.resolve(tmp_path / "data", {"id": "lab", "ssh_target": "lab-target_A"})
    assert settings["proxyjump"] == "lab-jump_A"
    assert settings["identityfiles"] == ["~/.ssh/existing-one", "~/.ssh/existing-two"]


# --- ssh password -----------------------------------------------------------------------------------------------------
def test_environment_has_optional_ttl_and_export_disables_it(tmp_path):
    store = Store(tmp_path / "db")
    env = store.save("environments", {"name": "Host", "kind": "ssh", "ssh_target": "work", "ssh_auth_mode": "password", "remember_seconds": 900})
    assert env["remember_seconds"] == 900
    path = tmp_path / "config.json"
    store.export_config(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert next(e for e in data["environments"] if e["id"] == env["id"])["remember_seconds"] == 0


def test_ttl_expiry_and_host_binding(monkeypatch):
    from vibe_launcher import askpass
    vault = {}
    monkeypatch.setattr(askpass, "windows_secret", lambda account, value=None: vault.setdefault(account["id"], value) if value else vault.get(account["id"]))
    monkeypatch.setattr(askpass, "delete_secret", lambda account: vault.pop(account["id"], None))
    profile = {"tool": "ssh", "id": "work"}
    askpass.save_password(profile, "private", "host1", 900, clock=100)
    assert askpass.cached_password(profile, "host1", 900, clock=999) == "private"
    assert askpass.cached_password(profile, "host2", 900, clock=999) is None
    assert askpass.cached_password(profile, "host1", 900, clock=1000) is None
    assert vault == {}


def test_only_exact_target_password_prompt_can_reuse_secret():
    from vibe_launcher.askpass import is_password_prompt
    expected = "alice@real-host's password:"
    assert is_password_prompt(expected, expected)
    for prompt in ("Password:", "Verification code:", "alice@jump's password:", "Enter passphrase for key:", "OTP password:"):
        assert not is_password_prompt(prompt, expected)


def test_disabled_uses_original_terminal_without_touching_vault(tmp_path, monkeypatch):
    from vibe_launcher import askpass
    monkeypatch.setattr(askpass.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no SSH resolution")))
    assert askpass.ssh_environment({"id": "host", "ssh_target": "work", "remember_seconds": 0}, tmp_path) is None


def test_native_bridge_ignores_cwd_and_pythonpath_module_hijack(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from vibe_launcher.askpass import build_bridge
    package = tmp_path / "vibe_launcher"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "askpass.py").write_text("print('synthetic-test-response')", encoding="utf-8")
    bridge = build_bridge(tmp_path)
    env = {**os.environ, "VIBE_SSH_PYTHON": sys.executable, "PYTHONPATH": str(tmp_path)}
    result = subprocess.run([str(bridge.resolve()), "test prompt"], cwd=tmp_path, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    assert result.returncode == 1  # Real isolated module rejects missing launch context.
    assert result.stdout == b""
    assert result.stderr == b""


def test_forget_button_clears_only_selected_environment(tmp_path, qtbot, monkeypatch):
    from vibe_launcher import askpass
    from vibe_launcher.ui import MainWindow
    store = Store(tmp_path / "ui.db")
    store.save("environments", {"id": "host", "name": "Host", "kind": "ssh", "ssh_target": "work", "remember_seconds": 900})
    cleared = []
    monkeypatch.setattr(askpass, "delete_secret", lambda profile: cleared.append(profile))
    view = MainWindow(store, object())
    qtbot.addWidget(view)
    view.environments_list.setCurrentRow(0)
    view.forget_ssh_password()
    assert cleared == [{"tool": "ssh", "id": "host"}]


def test_native_bridge_propagates_cancel_without_output(tmp_path):
    import os
    import subprocess
    import sys
    from vibe_launcher.askpass import build_bridge
    package = tmp_path / "vibe_launcher"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "askpass.py").write_text("raise SystemExit(1)", encoding="utf-8")
    result = subprocess.run([str(build_bridge(tmp_path).resolve()), "OTP"], cwd=tmp_path,
                            env={**os.environ, "VIBE_SSH_PYTHON": sys.executable, "PYTHONPATH": str(tmp_path)},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    assert result.returncode == 1
    assert result.stdout == b""


def cache_context(tmp_path, monkeypatch):
    from vibe_launcher import askpass
    store = Store(tmp_path / "db.sqlite")
    host = store.save("environments", {"id": "host", "name": "Host", "kind": "ssh", "ssh_target": "work", "ssh_auth_mode": "password", "remember_seconds": 900})
    vault = {}
    def secret(profile, value=None):
        if value is not None:
            vault[profile["id"]] = value
        return vault.get(profile["id"])
    monkeypatch.setattr(askpass, "windows_secret", secret)
    monkeypatch.setattr(askpass, "delete_secret", lambda profile: vault.pop(profile["id"], None))
    monkeypatch.setattr(askpass.ssh_security, "host_binding", lambda *a: "verified-host-fingerprint")
    with askpass.security_lock(tmp_path, "host") as (_, epoch):
        pass
    env = {"VIBE_SSH_ROOT": str(tmp_path), "VIBE_SSH_ID": "host", "VIBE_SSH_ATTEMPT": "attempt",
           "VIBE_SSH_DATABASE": str(store.path), "VIBE_SSH_EPOCH": epoch,
           "VIBE_SSH_CONFIG_EPOCH": host["credential_epoch"], "VIBE_SSH_CACHE": "1", "VIBE_SSH_SETTINGS": "{}"}
    return store, host, vault, env


def test_password_only_promoted_after_authenticated_channel(tmp_path, monkeypatch):
    from vibe_launcher import askpass
    store, host, vault, env = cache_context(tmp_path, monkeypatch)
    askpass.password_operation(env, "synthetic-中文")
    assert "host" not in vault
    assert askpass.password_operation(env) is None
    askpass.password_operation(env, promote=True)
    assert askpass.password_operation(env) == "synthetic-中文"
    assert "attempt" not in vault


def test_lock_invalidates_pending_and_old_windows(tmp_path, monkeypatch):
    from vibe_launcher import askpass
    store, host, vault, env = cache_context(tmp_path, monkeypatch)
    askpass.password_operation(env, "synthetic")
    askpass.revoke_password("host", tmp_path)
    askpass.password_operation(env, promote=True)
    assert "host" not in vault
    before = dict(vault)
    askpass.password_operation(env, "old-window-input")
    assert vault == before
    assert askpass.password_operation(env) is None


def test_policy_disable_then_reenable_never_revives_old_window(tmp_path, monkeypatch):
    from vibe_launcher import askpass
    store, host, vault, env = cache_context(tmp_path, monkeypatch)
    askpass.password_operation(env, "synthetic")
    host["remember_seconds"] = 0
    host = store.save("environments", host)
    host["remember_seconds"] = 900
    store.save("environments", host)
    askpass.password_operation(env, promote=True)
    assert "host" not in vault
    assert askpass.password_operation(env) is None


def test_otp_or_unverified_host_never_uses_cache(tmp_path, monkeypatch):
    from vibe_launcher import askpass
    store, host, vault, env = cache_context(tmp_path, monkeypatch)
    askpass.password_operation(env, "synthetic")
    askpass.password_operation(env, promote=True)
    assert askpass.password_operation({**env, "VIBE_SSH_CACHE": "0"}) is None
    monkeypatch.setattr(askpass.ssh_security, "host_binding", lambda *a: None)
    assert askpass.password_operation(env) is None


def test_replaced_host_binding_never_receives_saved_password(tmp_path, monkeypatch):
    from vibe_launcher import askpass
    store, host, vault, env = cache_context(tmp_path, monkeypatch)
    askpass.password_operation(env, "synthetic")
    askpass.password_operation(env, promote=True)
    monkeypatch.setattr(askpass.ssh_security, "host_binding", lambda *a: "different-host-fingerprint")
    assert askpass.password_operation(env) is None


def test_existing_bridge_binary_is_never_reused(tmp_path):
    from vibe_launcher.askpass import build_bridge
    first = build_bridge(tmp_path)
    first.write_bytes(b"replaced executable")
    second = build_bridge(tmp_path)
    assert first != second
    assert second.read_bytes().startswith(b"MZ")


# --- security ---------------------------------------------------------------------------------------------------------
def records(tmp_path):
    store = Store(tmp_path / "state.db")
    account = store.save("accounts", {"id": "account", "name": "A", "tool": "codex", "auth": "subscription"})
    env = store.save("environments", {"id": "host", "name": "Host", "kind": "ssh", "ssh_target": "work"})
    for sid in ("one", "two"):
        store.save("sessions", {"id": sid, "account_id": account["id"], "environment_id": "host", "name": sid,
                                "path": "/tmp", "status": "queued"})
    return store, account, env


def test_terminal_cannot_forge_auth_or_other_session(tmp_path, monkeypatch):
    store, account, env = records(tmp_path)
    request = {"action": "launch", "account": account, "environment_id": "host", "session_id": "one", "nonce": "public"}
    forged = b'VIBE_EVENT_public:{"session_id":"two","status":"ended","auth_status":"authenticated"}\n'
    class FakeSSH:
        stdout = io.BytesIO(forged)
        def wait(self):
            return 0
    monkeypatch.setattr(runner, "ssh_args", lambda *a, **k: ["fake-ssh"])
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeSSH())
    assert runner.run_remote(store, request, env) == 0
    assert store.get("sessions", "two")["status"] == "queued"
    assert store.get("sessions", "one")["status"] == "unknown"
    assert store.list("auth_states") == []


def test_event_session_binding_validation_and_terminal_state(tmp_path):
    store, account, env = records(tmp_path)
    request = {"action": "launch", "account": account, "environment_id": "host", "session_id": "one"}
    for event in ({"session_id": "two", "status": "ended"}, {"status": []}, {"pid": True, "status": "running"}, {"status": "invented"}):
        runner.record_event(store, request, event)
    assert all(s["status"] == "queued" for s in store.list("sessions"))
    runner.record_event(store, request, {"status": "ended"})
    runner.record_event(store, request, {"status": "running"})
    assert store.get("sessions", "one")["status"] == "ended"


def test_failed_report_does_not_commit_partial_states(tmp_path, monkeypatch):
    store, account, env = records(tmp_path)
    class FakeSSH:
        stdout = io.BytesIO(b'VIBE_EVENT_n:{"session_id":"one","status":"ended"}\n')
        def wait(self):
            return 255
    monkeypatch.setattr(runner, "ssh_args", lambda *a, **k: ["fake-ssh"])
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeSSH())
    runner.run_remote(store, {"action": "list-sessions", "environment_id": "host", "nonce": "n"}, env)
    assert store.get("sessions", "one")["status"] == "unknown"


def test_real_openssh_policy_overrides_unsafe_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh/config").write_text("Host work\n HostName 127.0.0.1\n User test\n StrictHostKeyChecking no\n ForwardAgent yes\n ForwardX11 yes\n LocalForward 0.0.0.0:8765 localhost:8765\n ProxyJump gateway\n")
    runtime.protect_path(tmp_path / ".ssh/config")
    env = {"id": "host", "ssh_target": "work", "ssh_auth_mode": "password"}
    result = ssh_security.resolve(tmp_path / "state", env)
    assert result["stricthostkeychecking"] in {"ask", "2"}
    assert result["forwardagent"] == "no"
    assert result["forwardx11"] == "no"
    assert result["clearallforwardings"] == "yes"
    assert result["proxyjump"] == "gateway"
    assert "localforward" not in result
    args = ssh_args("work", {"action": "doctor"}, root=tmp_path / "state", environment=env)
    assert Path(args[0]).is_absolute()
    assert "PreferredAuthentications=password" in args
    assert "KbdInteractiveAuthentication=no" in args
    with pytest.raises(ValueError, match="without configured forwards"):
        ssh_args("work", {"action": "login", "account": {"tool": "codex"}, "login_method": "browser"}, root=tmp_path / "state", environment=env)


def test_api_key_does_not_follow_endpoint_or_revision(tmp_path, monkeypatch):
    account = {"id": "api", "tool": "codex", "auth": "api", "model": "m", "base_url": "https://one.example", "credential_revision": "one"}
    vault = {}
    def secret(account, value=None):
        if value is not None:
            vault[account["id"]] = value
        return vault.get(account["id"])
    monkeypatch.setattr(runtime, "WINDOWS", True)
    monkeypatch.setattr(runtime, "windows_secret", secret)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(runtime.getpass, "getpass", lambda *a: "synthetic-key")
    assert runtime.api_key(account, tmp_path) == "synthetic-key"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert runtime.api_key(account, tmp_path) == "synthetic-key"
    for changed in ({**account, "base_url": "https://two.example"}, {**account, "credential_revision": "two"}):
        with pytest.raises(RuntimeError, match="interactive"):
            runtime.api_key(changed, tmp_path)


def test_linked_private_file_is_rejected_without_overwrite(tmp_path):
    target = tmp_path / "outside"
    target.write_text("untouched")
    link = tmp_path / "linked"
    os.link(target, link)
    with pytest.raises(ValueError, match="hard links"):
        runtime.write_json(link, {"overwrite": True})
    assert target.read_text() == "untouched"


def test_database_link_rejected_before_sqlite_opens(tmp_path):
    target = tmp_path / "outside"
    target.write_text("not a database")
    link = tmp_path / "db"
    os.link(target, link)
    with pytest.raises(ValueError, match="hard links"):
        Store(link)
    assert target.read_text() == "not a database"


def test_symbolic_data_directory_rejected(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Windows developer mode/symlink privilege unavailable")
    with pytest.raises(ValueError, match="symlinks"):
        runtime.private_dir(link / "helper")
    assert not (target / "helper").exists()


def test_project_executable_and_relative_path_cannot_hijack_cli(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "codex.exe").write_bytes(b"not an executable")
    (project / "codex.cmd").write_text("echo untrusted")
    monkeypatch.chdir(project)
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(project)]))
    with pytest.raises(RuntimeError, match="native executable"):
        runtime.trusted_executable("codex")


def test_failed_ssh_never_promotes_pending_password(tmp_path, monkeypatch):
    store, account, env = records(tmp_path)
    promoted, cleaned = [], []
    class Rejected:
        stdout = io.BytesIO(b"")
        def wait(self):
            return 255
    monkeypatch.setattr(runner, "ssh_args", lambda *a, **k: ["fake-ssh"])
    monkeypatch.setattr(runner.askpass, "ssh_environment", lambda *a: {"VIBE_SSH_ROOT": str(tmp_path), "VIBE_SSH_ATTEMPT": "fake"})
    monkeypatch.setattr(runner.askpass, "password_operation", lambda *a, **k: promoted.append(True))
    monkeypatch.setattr(runner.askpass, "cleanup", lambda env: cleaned.append(env))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Rejected())
    request = {"action": "launch", "account": account, "environment_id": "host", "session_id": "one", "nonce": "n"}
    assert runner.run_remote(store, request, env) == 255
    assert not promoted
    assert len(cleaned) == 1
