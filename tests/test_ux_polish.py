"""Small UX guarantees: empty states that lead somewhere, labels that say what a button does, keyboard on lists."""
import pytest
from PySide6.QtCore import Qt

from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow

from test_env_ui import FakeService


@pytest.fixture
def view(tmp_path, qtbot):
    store = Store(tmp_path / "gui.db")
    env = store.save("environments", {"name": "伺服器", "kind": "ssh", "ssh_target": "srv"})
    store.save("presets", {"name": "前端", "environment_id": env["id"], "path": "/w/web", "favorite": True})
    store.save("presets", {"name": "後端", "environment_id": env["id"], "path": "/w/api"})
    window = MainWindow(store, FakeService())
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
    window = MainWindow(store, FakeService())
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
