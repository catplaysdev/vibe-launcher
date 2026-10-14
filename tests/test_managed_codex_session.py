"""Worker control regressions with fake quota/bridge inputs and a deterministic clock."""
import copy
import json

import pytest

from vibe_launcher import runtime


@pytest.fixture(autouse=True)
def english(restore_controls):
    """These tests read English messages (the suite otherwise runs with the Traditional Chinese runtime catalog)."""
    from vibe_launcher import i18n, runtime as engine
    i18n.set_language("en")
    engine._MESSAGES = {}


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
