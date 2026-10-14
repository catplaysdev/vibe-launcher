"""The main window: pages, menus, keyboard navigation, home, settings and localization.

- ui
- ux polish: Small UX guarantees: empty states that lead somewhere, labels that say what a button does, keyboard on lists.
- context menus: Right-click menus offer the same actions as each page's buttons, at the pointer.
- navigation: Keyboard focus navigation overlay: numbered live outlines, digit jumps, no screenshots.
- home and settings: Home page, the settings window, movable messages and in-dialog validation.
- localization settings: Catalogs, configurable input navigation and terminal command profiles.
"""
import json
import re
from string import Formatter
from test_environments import FakeService as PolishFakeService
from test_workspace import FakeTerminal, SpawnService
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout,
                               QWidget)
from vibe_launcher import i18n, runtime, settings, shortcuts, terminal, theme, ui
from vibe_launcher.composer import chord_for
from vibe_launcher.home_page import HomePage
from vibe_launcher.navigation import cycle_window, FocusNavigation
from vibe_launcher.prefs import UiPrefs
from vibe_launcher.service import LauncherService
from vibe_launcher.settings_dialog import SettingsDialog
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow, RecordDialog
from vibe_launcher.workspace_page import TabInfo


# --- ui ---------------------------------------------------------------------------------------------------------------
class FakeService:
    def __init__(self):
        self.calls = []

    def start(self, action, **kwargs):
        kwargs.pop("workspace_id", None)      # which tmux session a launch joins is tested in test_workspace
        self.calls.append((action, kwargs))


@pytest.fixture
def window(tmp_path, qtbot):
    store = Store(tmp_path / "gui.db")
    account = store.save("accounts", {"name": "工作帳號", "tool": "codex", "auth": "subscription"})
    preset = store.save("presets", {"name": "工程分析", "account_id": account["id"],
                                    "environment_id": "local", "path": "C:/中文 專案"})
    service = FakeService()
    view = MainWindow(store, service)
    qtbot.addWidget(view)
    view.show()
    return view, store, service, account, preset


def test_search_never_launches_hidden_project(window):
    view, store, service, account, preset = window
    view.search.setText("no-match")
    view.launch_project()
    assert service.calls == []


def test_favorite_and_account_details_refresh(window):
    view, store, service, account, preset = window
    assert "工作帳號" in view.project_info.text()
    view.toggle_favorite()
    assert view.project_title.text().startswith("★")
    assert store.get("presets", preset["id"])["favorite"]
    view.launch_project()
    assert service.calls == [("launch", {"preset_id": preset["id"], "account_id": account["id"]})]


def test_auth_status_preserves_selection(window):
    view, store, service, account, preset = window
    view.accounts_page.select("accounts", account["id"])
    store.save("auth_states", {"id": account["id"] + ":local", "status": "authenticated",
                               "checked_at": "2026-10-04T00:00:00+00:00"})
    view._poll()
    listing = next(found for key, found in view.accounts_page.lists.items() if key.startswith("codex/"))
    assert "已登入" in listing.currentItem().text()
    assert view.accounts_page.current_selection == ("accounts", account["id"])


def test_http_loopback_permission_defaults_off_and_is_explicit(qtbot):
    dialog = RecordDialog(
        "帳號",
        [("base_url", "API Base URL", "")],
        {"base_url": "http://127.0.0.1:8000/v1"},
        checkboxes=[("allow_insecure_loopback", "允許 HTTP loopback")],
    )
    qtbot.addWidget(dialog)
    assert dialog.values()["allow_insecure_loopback"] is False
    dialog.fields["allow_insecure_loopback"].setChecked(True)
    assert dialog.values()["allow_insecure_loopback"] is True


def test_security_page_explains_password_cache_boundary(window):
    view, *_ = window
    view.show_page("environments")
    text = view.security_status_label.text()
    assert "同一 Windows 使用者" in text
    assert "OTP" in text


def test_version_check_targets_selected_environment_without_account(window):
    view, store, service, *_ = window
    view.environments_list.setCurrentRow(0)
    environment = view._selected_record("environments")
    view.check_tool_versions()
    assert service.calls == [("check-tools", {"environment_id": environment["id"]})]


def test_version_result_appears_on_poll_without_reselecting_environment(window):
    from vibe_launcher import runtime
    view, store, *_ = window
    view.environments_list.setCurrentRow(0)
    environment = view._selected_record("environments")
    runtime.write_json(store.path.parent / "tool-versions" / (environment["id"] + ".json"),
                       {"reports": [{"tool":"codex", "installed":"0.160.0", "latest":"0.160.0",
                                     "source":"CLI updater", "status":"current", "path":"C:/tools/codex.exe"}]})
    view._poll()
    assert "0.160.0" in view.tool_versions_label.text()


def test_lock_and_forget_revoke_selected_environment_secret(window, monkeypatch):
    view, store, *_ = window
    environment = store.save("environments", {"name": "測試主機", "kind": "ssh", "ssh_target": "dev@example"})
    view.reload()
    for row in range(view.environments_list.count()):
        item = view.environments_list.item(row)
        if item.data(256) == environment["id"]:
            view.environments_list.setCurrentItem(item)
            break
    calls = []
    from vibe_launcher import askpass
    monkeypatch.setattr(askpass, "revoke_password", lambda environment_id, root: calls.append((environment_id, root)))
    view.lock_ssh_password()
    view.forget_ssh_password()
    assert calls == [(environment["id"], store.path.parent), (environment["id"], store.path.parent)]


# --- ux polish --------------------------------------------------------------------------------------------------------
@pytest.fixture
def view(tmp_path, qtbot):
    store = Store(tmp_path / "gui.db")
    env = store.save("environments", {"name": "伺服器", "kind": "ssh", "ssh_target": "srv"})
    store.save("presets", {"name": "前端", "environment_id": env["id"], "path": "/w/web", "favorite": True})
    store.save("presets", {"name": "後端", "environment_id": env["id"], "path": "/w/api"})
    window = MainWindow(store, PolishFakeService())
    qtbot.addWidget(window)
    window.resize(1280, 800)
    window.show()
    window.show_page("projects")
    window._confirm = lambda *a, **k: True
    return window


def test_filters_that_match_nothing_offer_to_clear_them(view):
    view.search.setText("不存在的專案")
    panel = view.project_panel
    assert panel.stack.currentWidget() is panel.empty and panel.empty.title.text() == "沒有符合的專案"
    assert panel.empty.primary.text() == "清除篩選"
    panel.empty.primary.click()
    assert view.search.text() == "" and view.project_list.count() == 2
    assert panel.stack.currentWidget() is panel.listing


def test_the_favourite_button_says_what_it_will_do(view):
    names = {view.project_list.item(i).data(Qt.ItemDataRole.UserRole): i for i in range(view.project_list.count())}
    favourite = next(p for p in view._records["presets"] if p.get("favorite"))
    view.project_list.setCurrentRow(names[favourite["id"]])
    assert view.favorite_button.text() == "取消收藏"
    other = next(p for p in view._records["presets"] if not p.get("favorite"))
    view.project_list.setCurrentRow(names[other["id"]])
    assert view.favorite_button.text() == "收藏"


def test_sessions_empty_state_leads_to_projects(view):
    view.show_page("sessions")
    view.sessions_panel.empty.primary.click()
    assert view.current_page() == "projects"


def test_a_project_needs_an_environment_first_and_says_so_without_a_blocking_box(tmp_path, qtbot):
    store = Store(tmp_path / "empty.db")
    window = MainWindow(store, PolishFakeService())
    qtbot.addWidget(window)
    window._records["environments"] = []
    asked, added = [], []
    window._confirm = lambda title, *a, **k: asked.append(title) or True
    window.add_record = lambda kind: added.append(kind)
    assert window.add_project() is False
    assert asked == ["先設定執行環境"] and added == ["environments"] and window.current_page() == "environments"


def test_enter_and_delete_work_on_the_project_list(view, qtbot, monkeypatch):
    launched, deleted = [], []
    monkeypatch.setattr(view, "launch_project", lambda: launched.append(1))
    monkeypatch.setattr(view, "delete_project", lambda: deleted.append(1))
    view.activateWindow()
    view.project_list.setFocus()
    view.project_list.setCurrentRow(0)
    qtbot.keyClick(view.project_list, Qt.Key.Key_Return)
    qtbot.keyClick(view.project_list, Qt.Key.Key_Delete)
    assert launched and deleted


# --- context menus ----------------------------------------------------------------------------------------------------
@pytest.fixture
def menu_view(tmp_path, qtbot, monkeypatch):
    store = Store(tmp_path / "menus.db")
    store.save("environments", {"id": "srv", "name": "Server", "kind": "ssh", "ssh_target": "srv"})
    store.save("accounts", {"id": "a", "name": "Codex A", "tool": "codex", "auth": "subscription", "environment_id": "srv"})
    store.save("presets", {"id": "p", "name": "Web", "account_id": "a", "environment_id": "srv", "path": "/w/web"})
    store.save("sessions", {"id": "s", "name": "Web", "preset_id": "p", "account_id": "a", "environment_id": "srv",
                            "path": "/w/web", "status": "detached"})
    window = MainWindow(store, object())
    qtbot.addWidget(window, before_close_func=lambda w: w.workspace.shutdown())
    window.resize(1280, 800)
    window.show()
    shown = []
    monkeypatch.setattr(window, "_context_menu", lambda actions, where: shown.append(actions))
    window.shown_menus = shown
    return window


def right_click(listing, row):
    item = next(listing.item(i) for i in range(listing.count()) if listing.item(i).data(Qt.ItemDataRole.UserRole) == row)
    listing.customContextMenuRequested.emit(listing.visualItemRect(item).center())


def texts(actions):
    return [action.text for action in actions]


def test_a_project_right_click_selects_it_and_offers_its_actions(menu_view, monkeypatch):
    menu_view.show_page("projects")
    edited = []
    monkeypatch.setattr(menu_view, "edit_project", lambda: edited.append(menu_view._selected_project))
    right_click(menu_view.project_list, "p")
    actions, = menu_view.shown_menus
    assert menu_view._selected_project == "p"
    assert actions[-1].danger and actions[-1].callback == menu_view.delete_project
    next(a for a in actions if a.icon == "pencil").callback()
    assert edited == ["p"]


def test_an_account_right_click_lists_only_the_buttons_that_apply(menu_view):
    menu_view.show_page("accounts")
    right_click(menu_view.accounts_page.lists["codex/srv"], ("accounts", "a"))
    actions, = menu_view.shown_menus
    assert menu_view.accounts_page.current_selection == ("accounts", "a")
    shown = {action for action, button in menu_view.account_buttons.items() if not button.isHidden()}
    assert len(actions) == len(shown) and "status" not in shown          # GitHub-only check is not offered
    assert actions[-1].danger                                            # delete last, apart


def test_a_session_right_click_offers_attach_stop_and_remove_as_the_toolbar_allows(menu_view):
    menu_view.show_page("sessions")
    menu_view.set_sessions_grouping("time")
    right_click(menu_view.sessions_list, "s")
    actions, = menu_view.shown_menus
    assert texts(actions) == [menu_view.session_attach_button.text(), menu_view.session_stop_button.text(), menu_view.session_remove_button.text()]
    assert [a.enabled for a in actions] == [menu_view.session_attach_button.isEnabled(), menu_view.session_stop_button.isEnabled(),
                                            menu_view.session_remove_button.isEnabled()]


def test_right_clicking_an_empty_area_opens_nothing(menu_view):
    menu_view.show_page("projects")
    menu_view.project_list.customContextMenuRequested.emit(QPoint(5, menu_view.project_list.viewport().height() - 2))
    assert menu_view.shown_menus == []


def test_an_environment_right_click_opens_its_card_menu_at_the_pointer(menu_view, monkeypatch):
    menu_view.show_page("environments")
    page = menu_view.env_page
    opened = []
    monkeypatch.setattr(page, "_show_menu", lambda actions, anchor: opened.append((actions, anchor)))
    right_click(page.environments_list, "srv")
    (actions, anchor), = opened
    assert page.selected_id() == "srv" and isinstance(anchor, QPoint)
    assert len(actions) == len(page._env_actions(page._states["srv"]))


# --- navigation -------------------------------------------------------------------------------------------------------
class Pane(QWidget):
    def __init__(self):
        super().__init__()
        self.terminal = QFrame()
        self.terminal.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.terminal.setMinimumSize(200, 150)
        self.composer = SimpleNamespace(edit=QPlainTextEdit())
        layout = QVBoxLayout(self)
        layout.addWidget(self.terminal)
        layout.addWidget(self.composer.edit)

    def composer_visible(self):
        return self.composer.edit.isVisible()

    def set_composer_visible(self, visible):
        self.composer.edit.setVisible(visible)


class Workspace:
    def __init__(self, tabs):
        self._tabs = tabs
        self.selected = tabs[0].info.id
        self.area = SimpleNamespace(on_screen=lambda pane: True, mode="grid")

    def tabs(self):
        return list(self._tabs)

    def current_tabs(self):
        return self.tabs()

    def select(self, tab_id, focus=True):
        self.selected = tab_id

    def selected_id(self):
        return self.selected

    def tab(self, tab_id):
        return next((t for t in self._tabs if t.info.id == tab_id), None)


class FakeWindow(QWidget):
    def __init__(self):
        super().__init__()
        layout = QHBoxLayout(self)
        tabs = []
        for i, name in enumerate(("Codex · account", "Claude · other")):
            pane = Pane()
            layout.addWidget(pane)
            tabs.append(SimpleNamespace(info=SimpleNamespace(id=f"t{i}", label=name), pane=pane, terminal=pane.terminal))
        self.workspace = Workspace(tabs)
        self.resize(900, 600)

    def current_page(self):
        return "workspace"

    def centralWidget(self):  # noqa: N802
        return self


@pytest.fixture
def nav_window(qtbot, monkeypatch):
    def no_grab(*args, **kwargs):
        raise AssertionError("navigation must not take screenshots")
    monkeypatch.setattr(QWidget, "grab", no_grab)
    i18n.set_language("en")
    w = FakeWindow()
    qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    return w


def test_overlay_numbers_every_candidate_and_names_them(nav_window):
    tabs = nav_window.workspace.tabs()
    tabs[0].terminal.setFocus()
    nav = FocusNavigation(nav_window)
    nav.start()
    assert nav.active and nav.overlay.isVisible() and nav.target is tabs[0].terminal
    assert nav.overlay.geometry() == nav_window.rect()
    entries = nav.entries()
    assert [e[0] for e in entries] == [1, 2, 3, 4]
    assert [e[3] for e in entries] == ["Codex · account", "Codex · account · Input bar",
                                       "Claude · other", "Claude · other · Input bar"]
    # Painting goes through QPainter only (QWidget.grab is patched to fail): the hint bar sits at the top centre.
    image = QImage(nav_window.size(), QImage.Format.Format_ARGB32)
    image.fill(0)
    nav.overlay.render(image, QPoint())
    assert image.pixelColor(nav_window.width() // 2, 24).alpha() > 0
    assert image.pixelColor(nav_window.width() // 2, nav_window.height() - 20).alpha() == 0
    nav.stop()
    assert not nav.overlay.isVisible()


def test_digit_jumps_and_escape_restores_original_focus(nav_window, qtbot):
    tabs = nav_window.workspace.tabs()
    original = tabs[0].pane.composer.edit
    original.setFocus()
    nav = FocusNavigation(nav_window)
    nav.start()
    assert nav.target is original
    qtbot.keyClick(original, Qt.Key.Key_3)
    assert nav.target is tabs[1].terminal and nav_window.workspace.selected_id() == "t1"
    assert original.toPlainText() == ""
    qtbot.keyClick(original, Qt.Key.Key_9)  # no ninth target: nothing changes
    assert nav.target is tabs[1].terminal
    qtbot.keyClick(original, Qt.Key.Key_Escape)
    assert not nav.active and not nav.overlay.isVisible()
    assert QApplication.focusWidget() is original and nav_window.workspace.selected_id() == "t0"


def test_enter_confirms_and_overlay_follows_resize(nav_window, qtbot):
    tabs = nav_window.workspace.tabs()
    tabs[0].terminal.setFocus()
    nav = FocusNavigation(nav_window)
    nav.start()
    qtbot.keyClick(tabs[0].terminal, Qt.Key.Key_2)
    assert nav.target is tabs[0].pane.composer.edit
    nav_window.resize(1000, 700)
    assert nav.overlay.geometry() == nav_window.rect()
    qtbot.keyClick(tabs[0].terminal, Qt.Key.Key_Return)
    assert not nav.active and not nav.overlay.isVisible()
    assert QApplication.focusWidget() is tabs[0].pane.composer.edit


# --- home and settings ------------------------------------------------------------------------------------------------
@pytest.fixture
def home_view(tmp_path, qtbot):
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


def test_logo_opens_home_and_it_is_the_start_page(home_view):
    assert home_view.current_page() == "home"
    home_view.show_page("projects")
    home_view.sidebar.brand.click()
    assert home_view.current_page() == "home" and home_view.sidebar.brand.current
    assert home_view.home_page.recent.count() == 1


def test_settings_window_pages_conflicts_and_search(home_view):
    dialog = SettingsDialog(home_view)
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


def test_settings_appearance_previews_and_cancel_restores(home_view):
    before = home_view.appearance_settings()
    dialog = SettingsDialog(home_view, "appearance")
    dialog.appearance.cards["dracula"].click()
    assert theme.C.theme_id == "dracula"
    dialog.reject()
    assert theme.C.theme_id == before["theme"]
    dialog = SettingsDialog(home_view, "appearance")
    dialog.appearance.cards["nord"].click()
    dialog.save()
    assert home_view.prefs.theme == "nord" and theme.C.theme_id == "nord"


def test_messages_sit_top_centre_can_move_and_errors_stay(home_view, qtbot):
    toast = home_view.toasts.show("hello")
    qtbot.waitUntil(lambda: toast.slide.state() == toast.slide.State.Stopped, timeout=2000)
    host = home_view.toasts.host
    assert abs(toast.x() + toast.width() / 2 - host.width() / 2) <= 2 and toast.y() < host.height() / 4
    saved = []
    home_view.toasts.moved.connect(saved.append)
    toast.dragged.emit(toast, QPoint(40, host.height() // 2))
    toast.drag_finished.emit()
    assert saved and saved[-1][1] > 0.3 and abs(toast.y() - host.height() // 2) <= 2
    home_view.toasts.reset_position()
    assert saved[-1] is None
    error = home_view.toasts.show("broken", "danger")
    assert not error._timer.isActive()                           # stays until closed
    info = home_view.toasts.show("note")
    assert info._timer.isActive()
    info.enterEvent(None)
    assert not info._timer.isActive()                            # paused while read


def test_record_dialog_refuses_inside_the_dialog(home_view):
    dialog = RecordDialog("Test", [("name", "Name", "")], {}, None, home_view,
                          validate=lambda values: "" if values["name"] else "Name is required")
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted and dialog.error.text() == "Name is required"
    dialog.fields["name"].setText("ok")
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted


def session_ids(home_view):
    return [home_view.sessions_list.item(r).data(256) for r in range(home_view.sessions_list.count())]


def test_sessions_grouped_by_workspace_then_project(home_view, monkeypatch):
    home_view.store.save("presets", {"id": "old", "name": "Old project", "environment_id": "local", "path": "C:/old"})
    home_view.store.save("environments", {"id": "srv", "name": "Server", "kind": "ssh", "ssh_target": "user@server"})
    home_view.store.save("workspaces", {"id": "site", "name": "Site", "slots": [{"preset_id": "project", "kind": "agent"}]})
    rows = (("a", "project", "running", "2026-10-05T03:00:00+00:00"), ("b", "project", "ended", "2026-10-05T02:00:00+00:00"),
            ("c", "old", "ended", "2026-10-05T04:00:00+00:00"), ("d", "", "unknown", "2026-10-05T01:00:00+00:00"))
    for sid, preset, status, when in rows:
        home_view.store.save("sessions", {"id": sid, "preset_id": preset, "account_id": "user", "environment_id": "srv" if sid == "a" else "local",
                                     "name": sid, "path": "C:/p", "status": status, "updated_at": when})
    home_view.reload()
    assert home_view.prefs.sessions_grouping == "workspace"
    # Workspace > project > terminals: live work first, a project with only finished work is folded, nothing is lost.
    assert headings(home_view) == ["ws:site", "site/project", "ws:@none", "@none/__other__", "@none/old"]
    assert [i for i in session_ids(home_view) if i] == ["a", "b", "d"]
    home_view._toggle_session_group("@none/old")
    assert "c" in session_ids(home_view)
    home_view._toggle_session_group("site/project")
    assert "a" not in session_ids(home_view)
    home_view._toggle_session_group("ws:@none")
    assert headings(home_view) == ["ws:site", "site/project", "ws:@none"] and not [i for i in session_ids(home_view) if i]
    home_view.session_filter.set_current("finished")            # asking for finished work unfolds it
    home_view._session_collapsed.clear()
    home_view._render_sessions()
    assert set(i for i in session_ids(home_view) if i) == {"b", "c"}
    home_view.session_filter.set_current("all")
    cleared = []
    monkeypatch.setattr(home_view, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(home_view, "_delete_many", lambda kind, ids: cleared.extend(ids))
    home_view.clean_finished_sessions("project")
    assert cleared == ["b"]                                 # only that project's finished work
    recalled = []
    monkeypatch.setattr(home_view, "restore_project", recalled.append)
    home_view._session_collapsed.clear()
    home_view._render_sessions()
    project = home_view.sessions_list.itemWidget(home_view.sessions_list.item(1))
    [b for b in project.findChildren(QPushButton) if b.text() == ui.tr("Recall all")][0].click()
    assert recalled == ["project"]
    space = home_view.sessions_list.itemWidget(home_view.sessions_list.item(0))
    [b for b in space.findChildren(QPushButton) if b.text() == ui.tr("Switch")][0].click()
    assert home_view.workspace.workspace_id == "site" and home_view.current_page() == "workspace"
    home_view.session_grouping.set_current("time")
    assert not headings(home_view) and UiPrefs(home_view.prefs.path).sessions_grouping == "time"


def test_an_open_terminal_puts_its_session_under_that_workspace(home_view):
    home_view.store.save("workspaces", {"id": "site", "name": "Site", "slots": [{"preset_id": "project", "kind": "agent"}]})
    home_view.reload()
    home_view.open_terminal("project", "shell")                  # opened on the default desktop, though the project is also in Site
    tab = home_view.workspace.current_tabs()[0]
    home_view.store.save("sessions", {"id": tab.info.session_id, "preset_id": "project", "account_id": "user", "environment_id": "local",
                                 "name": "shell", "path": "C:/p", "status": "running"})
    home_view.reload()
    home_view.show_page("sessions")
    assert headings(home_view)[:2] == ["ws:", "/project"]


def headings(home_view):
    from vibe_launcher.kit import GROUP_ROLE
    return [home_view.sessions_list.item(r).data(GROUP_ROLE) for r in range(home_view.sessions_list.count())
            if home_view.sessions_list.item(r).data(GROUP_ROLE) is not None]


def test_sessions_filter_and_single_toolbar(home_view):
    home_view.set_sessions_grouping("time")
    for n, status in enumerate(("running", "unknown", "ended")):
        home_view.store.save("sessions", {"id": f"s{n}", "preset_id": "project", "account_id": "user", "environment_id": "local",
                                     "name": f"Work {n}", "path": "C:/p", "status": status})
    home_view.reload()
    home_view.show_page("sessions")
    assert home_view.sessions_list.count() == 3
    home_view.session_filter.set_current("unknown")
    assert home_view.sessions_list.count() == 1
    home_view.session_filter.set_current("finished")
    assert home_view.sessions_list.count() == 1
    home_view.session_filter.set_current("all")
    assert home_view.sessions_list.count() == 3


def test_starting_an_update_closes_the_settings_window_so_the_progress_is_not_covered(home_view, monkeypatch):
    from vibe_launcher import selfcmd, update_dialog
    monkeypatch.setattr(selfcmd, "frozen", lambda: True)
    monkeypatch.setattr(update_dialog.UpdateDialog, "start", lambda self: None)
    monkeypatch.setattr(home_view, "_confirm", lambda *a, **k: True)
    settings = SettingsDialog(home_view, "about")
    settings.show()
    assert settings.isVisible()
    home_view.apply_update()
    assert not settings.isVisible() and home_view._update_dialog.isVisible()
    home_view._update_dialog.close()


def test_the_installed_version_shows_without_checking_and_stale_results_are_ignored(home_view, monkeypatch):
    from vibe_launcher import updates
    from vibe_launcher.about import AboutPanel
    monkeypatch.setattr(updates, "current_version", lambda: "0.2.1")
    home_view._update_report = None
    updates.runtime.write_json(home_view.store.path.parent / updates.CACHE_NAME, {
        "status": "update_available", "current": "0.2.0", "latest": "0.2.1", "checked_at": "t", "message": "",
        "releases": [{"version": "0.2.1", "date": "", "notes": []}]})
    assert home_view.update_report() is None, "a result recorded by an older version is not shown"
    panel = AboutPanel(home_view)
    assert "0.2.1" in panel.version.text() and "0.2.0" not in panel.version.text()
    assert "0.2.1" in home_view.windowTitle() or home_view.windowTitle().startswith("Vibe Launcher")


def test_repeated_update_requests_reuse_the_active_dialog(home_view, monkeypatch):
    from vibe_launcher import selfcmd, update_dialog
    monkeypatch.setattr(selfcmd, "frozen", lambda: True)
    starts = []
    monkeypatch.setattr(update_dialog.UpdateDialog, "start", lambda dialog: starts.append(dialog))
    monkeypatch.setattr(home_view, "_confirm", lambda *a, **k: True)
    home_view.apply_update()
    first = home_view._update_dialog
    monkeypatch.setattr(home_view, "_confirm", lambda *a, **k: pytest.fail("must reuse the running update"))
    home_view.apply_update()
    assert home_view._update_dialog is first and starts == [first]
    first._fail("Failed")
    first.close()


def test_confirmed_installer_update_closes_without_a_second_modal(home_view, monkeypatch):
    home_view.open_terminal("project", "shell")
    assert home_view.workspace.current_tabs()[0].alive()
    home_view.prefs.minimize_to_tray = True
    home_view._installer_started()
    monkeypatch.setattr(home_view, "_confirm", lambda *a, **k: pytest.fail("update already confirmed ending local terminals"))
    assert home_view.close() and not home_view.isVisible()


# --- localization settings --------------------------------------------------------------------------------------------
@pytest.fixture
def locale_view(tmp_path, qtbot):
    store = Store(tmp_path / "launcher.db")
    store.save("accounts", {"id": "user", "name": "Example", "tool": "codex", "auth": "subscription"})
    store.save("presets", {"id": "project", "name": "Example project", "environment_id": "local", "path": str(tmp_path)})
    window = MainWindow(store, SpawnService(), terminal_factory=FakeTerminal)
    qtbot.addWidget(window, before_close_func=lambda w: w.workspace.shutdown())
    window.show()
    return window


@pytest.mark.parametrize("locale", i18n.LANGUAGES)
def test_catalogs_cover_sources_and_keep_format_fields(locale):
    english, translated = i18n.catalog("en"), i18n.catalog(locale)
    assert english.keys() == translated.keys()
    assert len(english) >= 930
    for key in english:
        assert isinstance(english[key], str) and isinstance(translated[key], str) and translated[key].strip()
        assert "\ufffd" not in translated[key] and not re.search(r"\?{2,}", translated[key]), key
        if re.search(r"\{[a-zA-Z_]\w*(?:[:!][^{}]*)?\}", english[key]):
            names = lambda text: {name for _, name, _, _ in Formatter().parse(text) if name is not None}
            assert names(english[key]) == names(translated[key]), key
        for pattern in (r"</?[a-zA-Z][^<>]*>", r"%(?:[-+0# ]*\d*(?:\.\d+)?)?[sd]"):
            assert re.findall(pattern, english[key]) == re.findall(pattern, translated[key]), key
    i18n.set_language("en")
    assert i18n.tr("Terminal") == "Terminal"
    i18n.set_language("zh-TW")
    assert i18n.tr("Terminal") == "終端機"


def test_english_ui_and_named_fields(locale_view):
    i18n.set_language("en")
    dialog = SettingsDialog(locale_view)
    assert dialog.windowTitle() == "Settings"
    assert dialog.nav.item(0).text() == "General"
    assert "Enter confirm" in i18n.tr("navigation.hint", up="W", down="S", left="A", right="D", input="Tab")
    dialog.close()


def test_language_and_controls_survive_restart(tmp_path):
    prefs = UiPrefs(tmp_path / "ui-state.json")
    prefs.language = "en"
    prefs.shortcuts = {"next": "Ctrl+Alt+L"}
    prefs.composer_position = "top"
    prefs.save()
    restored = UiPrefs(prefs.path)
    assert (restored.language, restored.shortcuts, restored.composer_position) == ("en", {"next": "Ctrl+Alt+L"}, "top")


@pytest.mark.parametrize("locale", i18n.LANGUAGES)
def test_registered_language_is_selectable_and_survives_restart(locale_view, tmp_path, monkeypatch, locale):
    i18n.set_language(locale)
    dialog = SettingsDialog(locale_view)
    assert dialog.windowTitle() == i18n.catalog(locale)["Settings"]
    assert dialog.language.findData(locale) >= 0
    dialog.reject()
    monkeypatch.delenv("VIBE_LAUNCHER_LANGUAGE", raising=False)
    monkeypatch.setenv("VIBE_LAUNCHER_HOME", str(tmp_path))
    prefs = UiPrefs(tmp_path / "ui-state.json")
    prefs.language = locale
    prefs.save()
    assert UiPrefs(prefs.path).language == locale
    assert i18n._initial_language() == locale
    assert i18n.tr("untranslated-example") == "untranslated-example"


@pytest.mark.parametrize("locale", ("de", "es"))
def test_long_translations_fit_narrow_settings(locale_view, qtbot, locale):
    i18n.set_language(locale)
    dialog = SettingsDialog(locale_view)
    qtbot.addWidget(dialog)
    dialog.resize(680, 520)
    dialog.show()
    for page_name in ("general", "keyboard"):
        dialog.show_page(page_name)
        qtbot.wait(30)
        page = dialog.stack.currentWidget()
        if page.widget().width() > page.viewport().width():
            assert page.horizontalScrollBar().isVisible() and page.horizontalScrollBar().maximum() > 0
        if page_name == "keyboard":
            for edit in dialog.key_edits.values():
                page.ensureWidgetVisible(edit)
                QApplication.processEvents()
                left = edit.mapTo(page.viewport(), QPoint()).x()
                assert left >= 0 and left + edit.width() <= page.viewport().width()
    dialog.reject()


def test_language_defaults_to_english_and_reads_saved_choice(tmp_path, monkeypatch):
    monkeypatch.delenv("VIBE_LAUNCHER_LANGUAGE", raising=False)
    monkeypatch.setenv("VIBE_LAUNCHER_HOME", str(tmp_path))
    assert i18n._initial_language() == "en"
    prefs = UiPrefs(tmp_path / "ui-state.json")
    prefs.language = "zh-TW"
    prefs.save()
    assert i18n._initial_language() == "zh-TW"


def test_remote_runtime_embeds_only_needed_locale_without_package_imports():
    import base64
    import lzma
    from pathlib import Path
    from vibe_launcher.service import _compressed_runtime
    source = Path(runtime.__file__).read_bytes()
    for locale in i18n.LANGUAGES:
        _, compressed = _compressed_runtime(source, locale=locale)
        shipped = lzma.decompress(base64.b85decode(compressed)).decode("utf-8")
        namespace = {"__name__": "preview_runtime"}
        exec(compile(shipped, "preview_runtime", "exec"), namespace)
        assert "from vibe_launcher" not in shipped
        assert namespace["_MESSAGES"] == {}
        key = "Git author email format is invalid"
        assert repr(i18n.catalog(locale)[key]) in shipped


@pytest.mark.parametrize("locale", i18n.LANGUAGES)
@pytest.mark.parametrize("action", ("launch", "github", "harness"))
def test_remote_helper_translations_fit_transport_limit(locale, action):
    from vibe_launcher.service import remote_command
    request = {"action": action, "language": locale, "path": "/home/user/example"}
    if action == "launch":
        request["account"] = {"id": "example", "tool": "codex", "auth": "subscription"}
    elif action == "github":
        request.update(github_account={"id": "example", "environment_id": "ssh"}, github_operation="login", environment_id="ssh")
    else:
        request.update(account={"id": "example", "tool": "codex", "auth": "subscription"}, harness_operation="status", environment_id="ssh")
    assert len(remote_command(request)) <= 28000


@pytest.mark.parametrize("locale", i18n.LANGUAGES)
def test_github_login_metadata_fits_translated_remote_helper(tmp_path, locale):
    i18n.set_language(locale)
    store = Store(tmp_path / "launcher.db")
    store.save("environments", {"id": "ssh", "name": "Example", "kind": "ssh", "ssh_target": "example.com"})
    store.save("github_accounts", {"id": "example", "name": "Example", "environment_id": "ssh"})
    launches = []
    LauncherService(store).start("github", github_account_id="example", environment_id="ssh",
                                 operation={"github_operation": "login"}, spawn=lambda args, context: launches.append(args))
    assert launches


def test_custom_shortcut_hints_update_with_controls(locale_view):
    tab = locale_view.workspace.add_terminal(TabInfo(id="one", title="One", kind="shell", tool="shell"), FakeTerminal())
    locale_view.prefs.shortcuts = {"sidebar": "Ctrl+Alt+B", "composer": "Ctrl+Alt+E", "palette": "Ctrl+Alt+K"}
    locale_view.apply_controls()
    assert locale_view.sidebar.collapse_button._hint == "Ctrl+Alt+B"
    assert "Ctrl+Alt+E" in tab.pane.composer_button.toolTip()
    assert locale_view.workspace.palette_button.text() == "Ctrl+Alt+K"


def test_custom_keys_match_qt_and_web_and_keep_tmux_prefix():
    from PySide6.QtCore import QEvent
    shortcuts.configure({"next": "Ctrl+Alt+L"})
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_L, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
    assert chord_for(event) == "next"
    binding = next(b for b in shortcuts.web_bindings() if b["action"] == "next")
    assert binding == {"action": "next", "key": "l", "ctrl": True, "alt": True, "shift": False, "meta": False}
    assert all(not (b["key"] in {"b", "c"} and b["ctrl"] and not b["alt"] and not b["shift"]) for b in shortcuts.web_bindings())
    assert '__SHORTCUTS__' not in terminal.terminal_html()
    assert '"action": "next", "key": "l"' in terminal.terminal_html()
    with pytest.raises(ValueError, match="conflict"):
        shortcuts.validate({"next": "Ctrl+K"})
    with pytest.raises(ValueError):
        shortcuts.validate({"next": "L"})


@pytest.mark.skipif(__import__("os").environ.get("VIBE_TEST_WEBENGINE") != "1", reason="opt-in: starts a real Chromium")
def test_custom_keys_reach_the_real_terminal_bridge(qtbot):
    widget = terminal.TerminalWidget()
    qtbot.addWidget(widget, before_close_func=lambda w: w.dispose())
    widget.resize(900, 500)
    widget.show()
    qtbot.waitUntil(lambda: widget._page_ready, timeout=20000)
    actions = []
    widget.chord.connect(actions.append)
    shortcuts.configure({"next": "Ctrl+Alt+L"})
    widget.apply_shortcuts()
    widget._page.runJavaScript("""
        document.querySelector('.xterm-helper-textarea').dispatchEvent(
            new KeyboardEvent('keydown', {key: 'l', code: 'KeyL', ctrlKey: true, altKey: true,
                                         bubbles: true, cancelable: true}));
    """)
    qtbot.waitUntil(lambda: actions == ["next"], timeout=10000)
    widget.dispose()


def test_settings_save_reset_and_reject_conflicts(locale_view):
    dialog = SettingsDialog(locale_view)
    dialog.key_edits["next"].setKeySequence(QKeySequence("Ctrl+K"))
    dialog.save()
    assert "conflict" in dialog.error.text()
    assert not (locale_view.store.path.parent / "settings.json").exists()
    dialog.reset_shortcuts()
    dialog.position.setCurrentIndex(dialog.position.findData("top"))
    dialog.language.setCurrentIndex(dialog.language.findData("en"))
    dialog.save()
    assert locale_view.prefs.shortcuts == {} and locale_view.prefs.composer_position == "top"
    assert settings.load_settings(locale_view.store.path.parent)["shells"]["linux"] == "bash"


def test_navigation_moves_between_inputs_without_sending_text(locale_view, qtbot):
    locale_view.show_page("workspace")
    tab = locale_view.workspace.add_terminal(TabInfo(
        id="tab", title="Codex", kind="agent", tool="codex"), FakeTerminal())
    qtbot.wait(30)
    edit = tab.pane.composer.edit
    edit.setPlainText("unsent draft")
    edit.setFocus()
    nav = FocusNavigation(locale_view)
    nav.start()
    assert nav.active and nav.target is edit and nav.overlay.isVisible()
    nav.move("up")
    assert nav.target is tab.terminal
    qtbot.keyClick(edit, Qt.Key.Key_Tab)
    assert nav.target is edit
    qtbot.keyClick(edit, Qt.Key.Key_D)
    assert edit.toPlainText() == "unsent draft" and not tab.terminal.sent
    qtbot.keyClick(edit, Qt.Key.Key_Return)
    assert not nav.active and QApplication.focusWidget() is edit
    tab.pane.set_composer_position("top")
    nav.start()
    nav.move("down")
    assert nav.target is tab.terminal
    nav.stop(restore=True)


def test_navigation_switches_tabs_and_highlights_new_terminal(locale_view, qtbot):
    locale_view.show_page("workspace")
    one = locale_view.workspace.add_terminal(TabInfo(id="one", title="One", kind="shell", tool="shell"), FakeTerminal())
    two = locale_view.workspace.add_terminal(TabInfo(id="two", title="Two", kind="shell", tool="shell"), FakeTerminal())
    locale_view.workspace.select(one.info.id)
    qtbot.wait(30)
    nav = FocusNavigation(locale_view)
    nav.start()
    nav.move("right")
    assert nav.target is two.terminal and locale_view.workspace.selected_id() == "two"
    nav.stop()


def test_window_shortcut_activates_another_launcher(locale_view, qtbot, monkeypatch):
    other = MainWindow(locale_view.store, SpawnService(), terminal_factory=FakeTerminal)
    qtbot.addWidget(other, before_close_func=lambda w: w.workspace.shutdown())
    other.show()
    activated = []
    monkeypatch.setattr(other, "activateWindow", lambda: activated.append(True))
    cycle_window(locale_view, 1)
    assert activated == [True]


def test_terminal_profile_is_transported_and_saved(tmp_path):
    store = Store(tmp_path / "launcher.db")
    store.save("presets", {"id": "project", "name": "Example", "environment_id": "local", "path": str(tmp_path)})
    sid = LauncherService(store).start("launch", preset_id="project", mode="shell", terminal_profile="cmd", spawn=lambda *a: None)
    job = json.loads(next((tmp_path / "jobs").glob("*.json")).read_text(encoding="utf-8"))
    assert job["request"]["terminal_profile"]["command"] == ["cmd", "/K"]
    assert store.get("sessions", sid)["terminal_profile"] == "cmd"
    record = store.save("workspaces", {"name": "Example", "slots": [{"preset_id": "project", "kind": "shell", "terminal_profile": "cmd"}]})
    assert record["slots"][0]["terminal_profile"] == "cmd"


def test_plugins_validate_arguments_platforms_and_profile_isolation(tmp_path, monkeypatch):
    plugin = {"id": "example", "label": "Example CLI", "platforms": ["windows", "linux"],
              "command": ["example-agent", "--message", "hello; echo injected"], "profile_env": "EXAMPLE_AGENT_HOME"}
    settings.save_settings(tmp_path, {**settings.DEFAULT_SETTINGS, "plugins": [plugin]})
    assert settings.selected_profile(tmp_path, {"kind": "local"}, "example") == plugin
    monkeypatch.setattr(runtime, "trusted_executable", lambda name: name)
    assert runtime.shell_command(plugin) == plugin["command"]
    for changed in ({"command": "example-agent"}, {"profile_env": "HOME"}, {"command": ["./project-agent"]}, {"label": "a\nb"}):
        with pytest.raises(ValueError):
            runtime.validate_terminal_profile({**plugin, **changed})
    with pytest.raises(ValueError):
        runtime.validate_request({"action": "login", "mode": "shell", "terminal_profile": plugin})
    with pytest.raises(ValueError):
        settings.validate_settings({**settings.DEFAULT_SETTINGS, "plugins": [{k: v for k, v in plugin.items() if k != "profile_env"}]})
    monkeypatch.setattr(runtime, "WINDOWS", False)
    with pytest.raises(ValueError, match="incompatible"):
        runtime.shell_command(settings.BUILTIN_PROFILES[1])


def test_plugin_worker_uses_private_profile_and_preserves_argument_boundaries(tmp_path, monkeypatch):
    profile = {"id": "example", "label": "Example CLI", "platforms": ["windows", "linux"],
               "command": ["example-agent", "a; echo b"], "profile_env": "EXAMPLE_AGENT_HOME"}
    launches = []

    class Process:
        pid = 123
        def poll(self):
            return 0
        def wait(self):
            return 0

    monkeypatch.setattr(runtime, "trusted_executable", lambda name: name)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda args, **kw: launches.append((args, kw)) or Process())
    monkeypatch.setattr(runtime, "save_status", lambda *args, **kw: None)
    runtime.run_worker({"action": "worker", "mode": "shell", "session_id": "session", "path": str(tmp_path),
                        "terminal_profile": profile}, tmp_path, env={"PATH": "keep", "CODEX_HOME": "drop", "OPENAI_API_KEY": "example"})
    args, kwargs = launches[0]
    assert args == ["example-agent", "a; echo b"] and "shell" not in kwargs
    assert kwargs["env"] == {"PATH": "keep", "EXAMPLE_AGENT_HOME": str(tmp_path / "profiles" / "plugins" / "example")}
    assert (tmp_path / "profiles" / "plugins" / "example").is_dir()
