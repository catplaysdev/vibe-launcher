import io
import subprocess
from types import SimpleNamespace

import pytest

from vibe_launcher import askpass, runner, runtime
from vibe_launcher.store import Store

ACCOUNT = {"id": "a", "name": "A", "tool": "codex", "auth": "subscription"}


# ---------------------------------------------------------------- worker: Ctrl+C is a normal way to leave the CLI


def run_worker_with(tmp_path, monkeypatch, exit_code):
    monkeypatch.setattr(runtime, "emit", lambda *a: None)
    monkeypatch.setattr(runtime, "cli_command", lambda *a, **k: ["codex"])

    class Cli:
        pid = 4242

        def poll(self):
            return exit_code

        def wait(self):
            return exit_code
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: Cli())
    request = {"session_id": "s", "environment_id": "host", "path": str(tmp_path), "account": ACCOUNT}
    runtime.run_worker(request, tmp_path, {"PATH": "test"})
    return runtime.json.loads(runtime.read_private(runtime.session_path(tmp_path, "s")))["status"]


@pytest.mark.parametrize("code", [0, 130, 143, 129, -2, -15])
def test_quitting_the_cli_is_recorded_as_ended(tmp_path, monkeypatch, code):
    assert run_worker_with(tmp_path, monkeypatch, code) == "ended"


@pytest.mark.parametrize("code", [1, 2, 127, 137])
def test_a_crashing_cli_is_still_a_failure(tmp_path, monkeypatch, code):
    assert run_worker_with(tmp_path, monkeypatch, code) == "failed"


# ---------------------------------------------------------------- supervisor exit status


def outcome_for(tmp_path, monkeypatch, *, alive=False, record=None, verify=None, attach_code=0):
    emitted = []
    monkeypatch.setattr(runtime, "emit", lambda request, event: emitted.append(event["status"]))
    if verify is not None:
        monkeypatch.setattr(runtime, "tmux_session_exists", verify)
    else:
        monkeypatch.setattr(runtime, "tmux_session_exists", lambda session_id: alive)
    if record is not None:
        runtime.write_json(runtime.session_path(tmp_path, "s"), {"status": record})
    code = runtime.session_outcome({"session_id": "s"}, tmp_path, attach_code)
    return code, emitted


def test_session_still_in_tmux_is_reported_detached(tmp_path, monkeypatch):
    assert outcome_for(tmp_path, monkeypatch, alive=True) == (runtime.EXIT_SESSION_DETACHED, ["detached"])


def test_finished_session_is_reported_by_the_workers_own_record(tmp_path, monkeypatch):
    assert outcome_for(tmp_path, monkeypatch, record="ended") == (runtime.EXIT_SESSION_ENDED, ["ended"])
    assert outcome_for(tmp_path, monkeypatch, record="failed") == (runtime.EXIT_SESSION_FAILED, ["failed"])


def test_uncertain_cases_stay_unknown_and_keep_the_attach_status(tmp_path, monkeypatch):
    # dead session whose record still says "running" (e.g. the host rebooted)
    assert outcome_for(tmp_path, monkeypatch, record="running", attach_code=0) == (0, ["unknown"])
    # no record at all
    assert outcome_for(tmp_path / "nothing", monkeypatch, attach_code=1) == (1, ["unknown"])

    def cannot_verify(session_id):
        raise RuntimeError("Cannot verify tmux session state")
    assert outcome_for(tmp_path, monkeypatch, verify=cannot_verify, attach_code=0) == (0, ["unknown"])


# ---------------------------------------------------------------- runner trusts the exit status, not the terminal output


def setup(tmp_path, status="connecting"):
    store = Store(tmp_path / "state.db")
    account = store.save("accounts", ACCOUNT)
    env = store.save("environments", {"id": "host", "name": "H", "kind": "ssh", "ssh_target": "user@server"})
    store.save("sessions", {"id": "s", "name": "S", "account_id": "a", "environment_id": "host", "path": "/tmp", "status": status})
    return store, env, {"action": "launch", "nonce": "n", "session_id": "s", "environment_id": "host", "account": account}


def fake_ssh(monkeypatch, exit_code, output=b""):
    monkeypatch.setattr(askpass, "ssh_environment", lambda *a: None)
    monkeypatch.setattr(runner, "ssh_args", lambda *a, **k: ["fake-ssh"])

    class Remote:
        stdout = io.BytesIO(output)

        def wait(self):
            return exit_code
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: Remote())


@pytest.mark.parametrize("code, expected", [(runtime.EXIT_SESSION_ENDED, "ended"), (runtime.EXIT_SESSION_DETACHED, "detached"),
                                            (runtime.EXIT_SESSION_FAILED, "failed")])
@pytest.mark.parametrize("action", ["launch", "attach"])
def test_session_outcome_exit_status_sets_the_local_state(tmp_path, monkeypatch, code, expected, action):
    store, env, request = setup(tmp_path, status="connecting" if action == "launch" else "detached")
    request["action"] = action
    fake_ssh(monkeypatch, code)
    runner.run_remote(store, request, env)
    assert store.get("sessions", "s")["status"] == expected


def test_a_plain_exit_still_means_unknown_so_old_behaviour_is_conservative(tmp_path, monkeypatch):
    store, env, request = setup(tmp_path)
    fake_ssh(monkeypatch, 0)
    runner.run_remote(store, request, env)
    assert store.get("sessions", "s")["status"] == "unknown"


def test_cli_output_cannot_forge_the_outcome(tmp_path, monkeypatch):
    store, env, request = setup(tmp_path)
    fake_ssh(monkeypatch, 0, output=b'VIBE_EVENT_n:{"status": "ended"}\n')
    runner.run_remote(store, request, env)
    assert store.get("sessions", "s")["status"] == "unknown"


def test_outcome_codes_are_ignored_for_other_actions(tmp_path, monkeypatch):
    store, env, request = setup(tmp_path, status="running")
    request["action"] = "list-sessions"
    fake_ssh(monkeypatch, runtime.EXIT_SESSION_ENDED)
    runner.run_remote(store, request, env)
    assert store.get("sessions", "s")["status"] == "unknown"
