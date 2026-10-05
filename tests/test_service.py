import json
import subprocess

from vibe_launcher.store import Store
from vibe_launcher.service import LauncherService
from vibe_launcher.runner import record_event


def setup(store):
    account = store.save("accounts", {"name": "A", "tool": "codex", "auth": "subscription"})
    preset = store.save("presets", {"name": "Project", "account_id": account["id"],
                                    "environment_id": "local", "path": "C:/Project"})
    return account, preset


def test_launch_creates_immutable_nonsecret_request(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    account, preset = setup(store)
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)))
    LauncherService(store).start("launch", preset_id=preset["id"])
    job = json.loads(next((tmp_path / "jobs").glob("*.json")).read_text(encoding="utf-8"))
    account["name"] = "Changed"
    store.save("accounts", account)
    assert job["request"]["account"]["name"] == "A"
    assert job["request"]["path"] == "C:/Project"
    assert store.list("sessions")[0]["status"] == "queued"
    assert calls[0][1]["creationflags"] == subprocess.CREATE_NEW_CONSOLE


def test_status_events_do_not_mix_hosts(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    account, preset = setup(store)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: None)
    LauncherService(store).start("launch", preset_id=preset["id"])
    session = store.list("sessions")[0]
    request = {"account": account, "environment_id": "other", "session_id": session["id"]}
    record_event(store, request, {"status": "ended"})
    assert store.get("sessions", session["id"])["status"] == "queued"
    request["environment_id"] = "local"
    record_event(store, request, {"status": "ready", "auth_status": "configured"})
    assert store.get("sessions", session["id"])["status"] == "queued"
    assert store.list("auth_states")[0]["status"] == "configured"


def test_cancelled_login_does_not_stay_waiting(tmp_path):
    store = Store(tmp_path / "state.db")
    account, _ = setup(store)
    request = {"account": account, "environment_id": "local", "action": "login"}
    record_event(store, request, {"auth_status": "waiting_auth"})
    record_event(store, request, {"status": "unknown"})
    assert store.list("auth_states")[0]["status"] == "unknown"
