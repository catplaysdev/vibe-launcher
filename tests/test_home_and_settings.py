"""Home page, the settings window, movable messages and in-dialog validation."""
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QDialog, QPushButton

import pytest

from vibe_launcher import theme, ui
from vibe_launcher.home_page import HomePage
from vibe_launcher.prefs import UiPrefs
from vibe_launcher.settings_dialog import SettingsDialog
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow, RecordDialog
from test_workspace import FakeTerminal, SpawnService


@pytest.fixture
def view(tmp_path, qtbot):
    store = Store(tmp_path / "launcher.db")
    store.save("accounts", {"id": "user", "name": "Example", "tool": "codex", "auth": "subscription"})
    store.save("presets", {"id": "project", "name": "Example project", "environment_id": "local", "path": str(tmp_path),
                           "last_used": "2026-10-01T00:00:00+00:00"})
    window = MainWindow(store, SpawnService(), terminal_factory=FakeTerminal)
    qtbot.addWidget(window, before_close_func=lambda w: w.workspace.shutdown())
    window.resize(1180, 760)
    window.show()
    return window


RECORDS = {
    "presets": [{"id": "a", "name": "Site analysis", "environment_id": "local", "last_used": "2026-10-02T00:00:00+00:00"},
                {"id": "b", "name": "Frontend", "environment_id": "local", "last_used": "2026-10-03T00:00:00+00:00"}],
    "sessions": [{"id": "s1", "preset_id": "a", "status": "running"}, {"id": "s2", "preset_id": "b", "status": "unknown"},
                 {"id": "s3", "preset_id": "b", "status": "ended"}],
    "auth_states": [{"id": "x:local", "status": "logged_out"}],
    "github_accounts": [{"id": "g", "status": "unavailable"}],
    "environments": [{"id": "local", "name": "Local", "kind": "local"}],
    "accounts": [],
}


def test_home_counts_recent_projects_and_update_card(qtbot):
    page = HomePage()
    qtbot.addWidget(page)
    opened, restored = [], []
    page.open_requested.connect(opened.append)
    page.restore_requested.connect(restored.append)
    page.set_data(RECORDS, [], {"status": "update_available", "latest": "0.3.0", "releases": [{"version": "0.3.0"}, {"version": "0.2.0"}]})
    assert page.running.value.text() == "1" and page.attention.value.text() == "2" and page.unknown.value.text() == "1"
    assert not page.update_card.isHidden() and "0.3.0" in page.update_text.text()
    assert page.recent.count() == 2 and page.empty.isHidden()
    first = page.recent.itemAt(0).widget()                      # most recent first: Frontend, not running
    buttons = [b for b in first.findChildren(QPushButton) if b.text()]
    buttons[0].click()
    assert opened == ["b"]
    second = page.recent.itemAt(1).widget()
    [b for b in second.findChildren(QPushButton) if b.text()][0].click()
    assert restored == ["a"]                                     # running work is resumed, not opened again
    page.set_data({**RECORDS, "presets": []}, [], None)
    assert not page.empty.isHidden() and page.update_card.isHidden()


def test_logo_opens_home_and_it_is_the_start_page(view):
    assert view.current_page() == "home"
    view.show_page("projects")
    view.sidebar.brand.click()
    assert view.current_page() == "home" and view.sidebar.brand.current
    assert view.home_page.recent.count() == 1


def test_settings_window_pages_conflicts_and_search(view):
    dialog = SettingsDialog(view)
    assert dialog.nav.count() == len(SettingsDialog.PAGES) and not dialog.nav.item(0).icon().isNull()
    dialog.show_page("keyboard")
    assert dialog.stack.currentIndex() == 3
    dialog.key_edits["next"].setKeySequence(QKeySequence("Ctrl+K"))
    assert not dialog.save_button.isEnabled() and "conflict" in dialog.error.text()
    assert dialog.key_rows["next"][2].property("tone") == "danger"
    dialog.reset_shortcuts()
    assert dialog.save_button.isEnabled() and dialog.error.isHidden()
    dialog.key_search.setText("Alt+3")
    form, row, _ = dialog.key_rows["goto:3"]
    assert form.isRowVisible(row) and not dialog.key_rows["palette"][0].isRowVisible(dialog.key_rows["palette"][1])
    dialog.reject()


def test_settings_appearance_previews_and_cancel_restores(view):
    before = view.appearance_settings()
    dialog = SettingsDialog(view, "appearance")
    dialog.appearance.cards["dracula"].click()
    assert theme.C.theme_id == "dracula"
    dialog.reject()
    assert theme.C.theme_id == before["theme"]
    dialog = SettingsDialog(view, "appearance")
    dialog.appearance.cards["nord"].click()
    dialog.save()
    assert view.prefs.theme == "nord" and theme.C.theme_id == "nord"


def test_messages_sit_top_centre_can_move_and_errors_stay(view, qtbot):
    toast = view.toasts.show("hello")
    qtbot.waitUntil(lambda: toast.slide.state() == toast.slide.State.Stopped, timeout=2000)
    host = view.toasts.host
    assert abs(toast.x() + toast.width() / 2 - host.width() / 2) <= 2 and toast.y() < host.height() / 4
    saved = []
    view.toasts.moved.connect(saved.append)
    toast.dragged.emit(toast, QPoint(40, host.height() // 2))
    toast.drag_finished.emit()
    assert saved and saved[-1][1] > 0.3 and abs(toast.y() - host.height() // 2) <= 2
    view.toasts.reset_position()
    assert saved[-1] is None
    error = view.toasts.show("broken", "danger")
    assert not error._timer.isActive()                           # stays until closed
    info = view.toasts.show("note")
    assert info._timer.isActive()
    info.enterEvent(None)
    assert not info._timer.isActive()                            # paused while read


def test_record_dialog_refuses_inside_the_dialog(view):
    dialog = RecordDialog("Test", [("name", "Name", "")], {}, None, view,
                          validate=lambda values: "" if values["name"] else "Name is required")
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted and dialog.error.text() == "Name is required"
    dialog.fields["name"].setText("ok")
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted


def session_ids(view):
    return [view.sessions_list.item(r).data(256) for r in range(view.sessions_list.count())]


def test_sessions_grouped_by_workspace_then_project(view, monkeypatch):
    view.store.save("presets", {"id": "old", "name": "Old project", "environment_id": "local", "path": "C:/old"})
    view.store.save("environments", {"id": "srv", "name": "Server", "kind": "ssh", "ssh_target": "user@server"})
    view.store.save("workspaces", {"id": "site", "name": "Site", "slots": [{"preset_id": "project", "kind": "agent"}]})
    rows = (("a", "project", "running", "2026-10-05T03:00:00+00:00"), ("b", "project", "ended", "2026-10-05T02:00:00+00:00"),
            ("c", "old", "ended", "2026-10-05T04:00:00+00:00"), ("d", "", "unknown", "2026-10-05T01:00:00+00:00"))
    for sid, preset, status, when in rows:
        view.store.save("sessions", {"id": sid, "preset_id": preset, "account_id": "user", "environment_id": "srv" if sid == "a" else "local",
                                     "name": sid, "path": "C:/p", "status": status, "updated_at": when})
    view.reload()
    assert view.prefs.sessions_grouping == "workspace"
    # Workspace > project > terminals: live work first, a project with only finished work is folded, nothing is lost.
    assert headings(view) == ["ws:site", "site/project", "ws:@none", "@none/__other__", "@none/old"]
    assert [i for i in session_ids(view) if i] == ["a", "b", "d"]
    view._toggle_session_group("@none/old")
    assert "c" in session_ids(view)
    view._toggle_session_group("site/project")
    assert "a" not in session_ids(view)
    view._toggle_session_group("ws:@none")
    assert headings(view) == ["ws:site", "site/project", "ws:@none"] and not [i for i in session_ids(view) if i]
    view.session_filter.set_current("finished")            # asking for finished work unfolds it
    view._session_collapsed.clear()
    view._render_sessions()
    assert set(i for i in session_ids(view) if i) == {"b", "c"}
    view.session_filter.set_current("all")
    cleared = []
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(view, "_delete_many", lambda kind, ids: cleared.extend(ids))
    view.clean_finished_sessions("project")
    assert cleared == ["b"]                                 # only that project's finished work
    recalled = []
    monkeypatch.setattr(view, "restore_project", recalled.append)
    view._session_collapsed.clear()
    view._render_sessions()
    project = view.sessions_list.itemWidget(view.sessions_list.item(1))
    [b for b in project.findChildren(QPushButton) if b.text() == ui.tr("Recall all")][0].click()
    assert recalled == ["project"]
    space = view.sessions_list.itemWidget(view.sessions_list.item(0))
    [b for b in space.findChildren(QPushButton) if b.text() == ui.tr("Switch")][0].click()
    assert view.workspace.workspace_id == "site" and view.current_page() == "workspace"
    view.session_grouping.set_current("time")
    assert not headings(view) and UiPrefs(view.prefs.path).sessions_grouping == "time"


def test_an_open_terminal_puts_its_session_under_that_workspace(view):
    view.store.save("workspaces", {"id": "site", "name": "Site", "slots": [{"preset_id": "project", "kind": "agent"}]})
    view.reload()
    view.open_terminal("project", "shell")                  # opened on the default desktop, though the project is also in Site
    tab = view.workspace.current_tabs()[0]
    view.store.save("sessions", {"id": tab.info.session_id, "preset_id": "project", "account_id": "user", "environment_id": "local",
                                 "name": "shell", "path": "C:/p", "status": "running"})
    view.reload()
    view.show_page("sessions")
    assert headings(view)[:2] == ["ws:", "/project"]


def headings(view):
    from vibe_launcher.kit import GROUP_ROLE
    return [view.sessions_list.item(r).data(GROUP_ROLE) for r in range(view.sessions_list.count())
            if view.sessions_list.item(r).data(GROUP_ROLE) is not None]


def test_sessions_filter_and_single_toolbar(view):
    view.set_sessions_grouping("time")
    for n, status in enumerate(("running", "unknown", "ended")):
        view.store.save("sessions", {"id": f"s{n}", "preset_id": "project", "account_id": "user", "environment_id": "local",
                                     "name": f"Work {n}", "path": "C:/p", "status": status})
    view.reload()
    view.show_page("sessions")
    assert view.sessions_list.count() == 3
    view.session_filter.set_current("unknown")
    assert view.sessions_list.count() == 1
    view.session_filter.set_current("finished")
    assert view.sessions_list.count() == 1
    view.session_filter.set_current("all")
    assert view.sessions_list.count() == 3




def test_starting_an_update_closes_the_settings_window_so_the_progress_is_not_covered(view, monkeypatch):
    from vibe_launcher import selfcmd, update_dialog
    monkeypatch.setattr(selfcmd, "frozen", lambda: True)
    monkeypatch.setattr(update_dialog.UpdateDialog, "start", lambda self: None)
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    settings = SettingsDialog(view, "about")
    settings.show()
    assert settings.isVisible()
    view.apply_update()
    assert not settings.isVisible() and view._update_dialog.isVisible()
    view._update_dialog.close()


def test_the_installed_version_shows_without_checking_and_stale_results_are_ignored(view, monkeypatch):
    from vibe_launcher import updates
    from vibe_launcher.about import AboutPanel
    monkeypatch.setattr(updates, "current_version", lambda: "0.2.1")
    view._update_report = None
    updates.runtime.write_json(view.store.path.parent / updates.CACHE_NAME, {
        "status": "update_available", "current": "0.2.0", "latest": "0.2.1", "checked_at": "t", "message": "",
        "releases": [{"version": "0.2.1", "date": "", "notes": []}]})
    assert view.update_report() is None, "a result recorded by an older version is not shown"
    panel = AboutPanel(view)
    assert "0.2.1" in panel.version.text() and "0.2.0" not in panel.version.text()
    assert "0.2.1" in view.windowTitle() or view.windowTitle().startswith("Vibe Launcher")


def test_repeated_update_requests_reuse_the_active_dialog(view, monkeypatch):
    from vibe_launcher import selfcmd, update_dialog
    monkeypatch.setattr(selfcmd, "frozen", lambda: True)
    starts = []
    monkeypatch.setattr(update_dialog.UpdateDialog, "start", lambda dialog: starts.append(dialog))
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: True)
    view.apply_update()
    first = view._update_dialog
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: pytest.fail("must reuse the running update"))
    view.apply_update()
    assert view._update_dialog is first and starts == [first]
    first._fail("Failed")
    first.close()


def test_confirmed_installer_update_closes_without_a_second_modal(view, monkeypatch):
    view.open_terminal("project", "shell")
    assert view.workspace.current_tabs()[0].alive()
    view.prefs.minimize_to_tray = True
    view._installer_started()
    monkeypatch.setattr(view, "_confirm", lambda *a, **k: pytest.fail("update already confirmed ending local terminals"))
    assert view.close() and not view.isVisible()
