"""Right-click menus offer the same actions as each page's buttons, at the pointer."""
import pytest
from PySide6.QtCore import QPoint, Qt

from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow


@pytest.fixture
def view(tmp_path, qtbot, monkeypatch):
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


def test_a_project_right_click_selects_it_and_offers_its_actions(view, monkeypatch):
    view.show_page("projects")
    edited = []
    monkeypatch.setattr(view, "edit_project", lambda: edited.append(view._selected_project))
    right_click(view.project_list, "p")
    actions, = view.shown_menus
    assert view._selected_project == "p"
    assert actions[-1].danger and actions[-1].callback == view.delete_project
    next(a for a in actions if a.icon == "pencil").callback()
    assert edited == ["p"]


def test_an_account_right_click_lists_only_the_buttons_that_apply(view):
    view.show_page("accounts")
    right_click(view.accounts_page.lists["codex/srv"], ("accounts", "a"))
    actions, = view.shown_menus
    assert view.accounts_page.current_selection == ("accounts", "a")
    shown = {action for action, button in view.account_buttons.items() if not button.isHidden()}
    assert len(actions) == len(shown) and "status" not in shown          # GitHub-only check is not offered
    assert actions[-1].danger                                            # delete last, apart


def test_a_session_right_click_offers_attach_stop_and_remove_as_the_toolbar_allows(view):
    view.show_page("sessions")
    view.set_sessions_grouping("time")
    right_click(view.sessions_list, "s")
    actions, = view.shown_menus
    assert texts(actions) == [view.session_attach_button.text(), view.session_stop_button.text(), view.session_remove_button.text()]
    assert [a.enabled for a in actions] == [view.session_attach_button.isEnabled(), view.session_stop_button.isEnabled(),
                                            view.session_remove_button.isEnabled()]


def test_right_clicking_an_empty_area_opens_nothing(view):
    view.show_page("projects")
    view.project_list.customContextMenuRequested.emit(QPoint(5, view.project_list.viewport().height() - 2))
    assert view.shown_menus == []


def test_an_environment_right_click_opens_its_card_menu_at_the_pointer(view, monkeypatch):
    view.show_page("environments")
    page = view.env_page
    opened = []
    monkeypatch.setattr(page, "_show_menu", lambda actions, anchor: opened.append((actions, anchor)))
    right_click(page.environments_list, "srv")
    (actions, anchor), = opened
    assert page.selected_id() == "srv" and isinstance(anchor, QPoint)
    assert len(actions) == len(page._env_actions(page._states["srv"]))
