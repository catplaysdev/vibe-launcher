import json
import subprocess
from pathlib import Path

import pytest

from vibe_launcher import runtime
from vibe_launcher.service import LauncherService
from vibe_launcher.store import Store


def report(tool="codex", installed="0.114.0"):
    return {"tool":tool,"installed":installed,"latest":"0.160.0","path":"/snap/bin/codex", "source":"Snap",
            "status":"update_available","message":"","can_update":True,"checked_at":"test"}


def test_update_validates_tool_and_channel():
    with pytest.raises(ValueError):
        runtime.validate_request({"action":"update-tool","tool":"other"})
    with pytest.raises(ValueError):
        runtime.validate_request({"action":"update-tool","tool":"codex","snap_channel":"edge; bad"})


def test_cli_owned_updater_uses_exact_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "tool_output", lambda *a:"Commands:\n  update [options] Update tool\n")
    source, args, details = runtime.update_route("claude", str(tmp_path / "claude.exe"), tmp_path)
    assert args == [str(tmp_path / "claude.exe"), "update"]


def test_unknown_install_never_runs_install_script(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime,"tool_output",lambda *a:"Commands:\n  login\n")
    assert runtime.update_route("codex", str(tmp_path / "codex.exe"), tmp_path)[1] == []


def test_update_rechecks_path_and_version(tmp_path, monkeypatch):
    before = report()
    after = {**report(installed="0.160.0"), "path":"/new/bin/codex", "status":"current"}
    values = iter([before, after])
    monkeypatch.setattr(runtime,"inspect_tool",lambda *a:next(values))
    monkeypatch.setattr(runtime,"update_route",lambda *a:("CLI updater",["/old/bin/codex","update"],{}))
    calls=[]
    monkeypatch.setattr(runtime.subprocess,"call",lambda args,**kw:calls.append(args) or 0)
    assert runtime.manage_tools({"action":"update-tool","tool":"codex","environment_id":"host","nonce":"n"},tmp_path)==0
    saved=json.loads((tmp_path/"tool-versions/host.json").read_text())['reports'][0]
    assert saved['path']=="/new/bin/codex"
    assert saved['installed']=="0.160.0"
    assert saved['previous']=="0.114.0"
    assert calls==[["/old/bin/codex","update"]]


def test_updater_success_unchanged_version_is_not_claimed_updated(tmp_path,monkeypatch):
    monkeypatch.setattr(runtime,"inspect_tool",lambda *a:report())
    monkeypatch.setattr(runtime,"update_route",lambda *a:("CLI updater",["tool","update"],{}))
    monkeypatch.setattr(runtime.subprocess,"call",lambda *a,**k:0)
    runtime.manage_tools({"action":"update-tool","tool":"codex","environment_id":"host","nonce":"n"},tmp_path)
    saved=json.loads((tmp_path/"tool-versions/host.json").read_text())['reports'][0]
    assert "unchanged" in saved['message']
    assert saved['status']=="update_available"


def test_cancelled_or_failed_updater_does_not_mark_current(tmp_path,monkeypatch):
    monkeypatch.setattr(runtime,"inspect_tool",lambda *a:report())
    monkeypatch.setattr(runtime,"update_route",lambda *a:("CLI updater",["tool","update"],{}))
    monkeypatch.setattr(runtime.subprocess,"call",lambda *a,**k:1)
    assert runtime.manage_tools({"action":"update-tool","tool":"codex","environment_id":"host","nonce":"n"},tmp_path)==1
    saved=json.loads((tmp_path/"tool-versions/host.json").read_text())['reports'][0]
    assert saved['status']=="update_failed"


def test_unreachable_registry_is_unknown(tmp_path,monkeypatch):
    monkeypatch.setattr(runtime,"trusted_executable",lambda *a:"/bin/codex")
    monkeypatch.setattr(runtime,"tool_output",lambda *a:"codex-cli 0.114.0")
    monkeypatch.setattr(runtime,"update_route",lambda *a:("CLI updater",["tool","update"],{}))
    monkeypatch.setattr(runtime,"upstream_version",lambda *a:(_ for _ in ()).throw(OSError()))
    value=runtime.inspect_tool("codex",tmp_path)
    assert value['status']=="unknown"
    assert value['latest']=="unknown"


def test_check_does_not_invoke_updater(tmp_path,monkeypatch):
    monkeypatch.setattr(runtime,"inspect_tool",lambda tool,*a:report(tool))
    monkeypatch.setattr(runtime.subprocess,"call",lambda *a,**kw:pytest.fail("check must not update"))
    assert runtime.manage_tools({"action":"check-tools","environment_id":"host","nonce":"n"},tmp_path)==0


def test_service_update_is_environment_scoped(tmp_path,monkeypatch):
    store=Store(tmp_path/"db")
    monkeypatch.setattr(subprocess,"Popen",lambda *a,**kw:None)
    LauncherService(store).start("update-tool",environment_id="local",tool="codex")
    job=json.loads(next((tmp_path/"jobs").glob("*.json")).read_text(encoding="utf-8"))
    assert job['request']['tool']=="codex"
    assert 'account' not in job['request']
    assert store.list('sessions')==[]
