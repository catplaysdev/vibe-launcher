"""Picking a project folder on an SSH host: the listing action, the client that fetches it, and the picker dialog."""
import json
import os
import subprocess

import pytest
from PySide6.QtWidgets import QComboBox

from vibe_launcher import launch_dialogs, remote_browse, runtime, ui
from vibe_launcher.launch_dialogs import RemoteFolderDialog
from vibe_launcher.remote_browse import RemoteBrowseError


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
TREE = {
    "~": {"path": "/home/me", "exists": True, "home": "/home/me", "parent": "/home", "dirs": [".cache", "app", "docs"]},
    "/home/me": {"path": "/home/me", "exists": True, "home": "/home/me", "parent": "/home", "dirs": [".cache", "app", "docs"]},
    "/home/me/app": {"path": "/home/me/app", "exists": True, "home": "/home/me", "parent": "/home/me", "dirs": ["src"]},
    "/home": {"path": "/home", "exists": True, "home": "/home/me", "parent": "/", "dirs": ["me"]},
    "/home/me/new": {"path": "/home/me", "exists": False, "home": "/home/me", "parent": "/home", "dirs": ["app"]},
}


def lister(path):
    if path == "/boom":
        raise RemoteBrowseError("連線逾時。")
    return TREE[path]


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
    monkeypatch.setattr(remote_browse, "list_remote", lambda *a, **k: TREE["~"])
    assert window._browse_remote("gpu", "~/x") == "/home/me/app"
    assert opened and "GPU" in opened[0][0]
