from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from vibe_launcher.accounts_page import AccountsPage


ENVS = [{"id": "local", "name": "本機"}, {"id": "ssh", "name": "開發主機"}]
ACCOUNTS = [
    {"id": "c", "name": "個人 Codex", "tool": "codex", "auth": "subscription", "environment_id": "local"},
    {"id": "a", "name": "Claude API", "tool": "claude", "auth": "api", "environment_id": "", "model": "model"},
    {"id": "h", "name": "Harness", "tool": "deepseek-harness", "environment_id": "ssh"},
]
GITHUB = [{"id": "g", "name": "GitHub 個人", "login": "octocat", "status": "authenticated", "environment_id": "local"}]
AUTH = [{"id": "c:local", "status": "authenticated"}]


def page(qtbot, accounts=ACCOUNTS, github=GITHUB):
    widget = AccountsPage()
    qtbot.addWidget(widget)
    widget.set_records(accounts, github, ENVS, AUTH)
    widget.resize(1180, 760)
    widget.show()
    return widget


def test_grouped_rows_show_real_github_login_and_status(qtbot):
    widget = page(qtbot)
    assert list(widget.plates) == ["codex", "claude", "deepseek-harness", "github"]
    assert "octocat" in widget.lists["github/local"].item(0).text()
    assert "已登入" in widget.lists["codex/local"].item(0).text()
    assert "設定完成" in widget.lists["claude/"].item(0).text()
    assert "實驗性" in widget.lists["deepseek-harness/ssh"].item(0).text()


def test_empty_plates_hidden_and_first_account_add_menu(qtbot):
    widget = page(qtbot, [], [])
    assert not widget.plates
    assert widget.empty.isVisible()
    with qtbot.waitSignal(widget.add_requested) as signal:
        widget.add_menu.actions()[3].trigger()
    assert signal.args == ["github"]
    widget.set_records([], GITHUB, ENVS, [])
    assert list(widget.plates) == ["github"]


def test_search_environment_filter_and_one_empty_hint(qtbot):
    widget = page(qtbot)
    widget.search.setText("octocat")
    assert list(widget.plates) == ["github"]
    widget.search.clear()
    widget.environment.setCurrentIndex(widget.environment.findData("ssh"))
    assert list(widget.plates) == ["claude", "deepseek-harness"]
    widget.search.setText("沒有這個帳號")
    assert not widget.plates
    assert widget.empty.isVisible()
    assert "找不到" in widget.empty.text()


def test_identical_data_preserves_widgets_search_and_selection(qtbot):
    widget = page(qtbot)
    widget.select("github_accounts", "g")
    widget.search.setText("octocat")
    listing = widget.lists["github/local"]
    item = listing.currentItem()
    widget.set_records(ACCOUNTS, GITHUB, ENVS, AUTH)
    assert widget.lists["github/local"] is listing
    assert listing.currentItem() is item
    assert widget.search.text() == "octocat"
    assert widget.current_selection == ("github_accounts", "g")


def test_selection_unique_and_action_matches_identity(qtbot):
    widget = page(qtbot)
    widget.add_detail_widget(QLabel("帳號操作"))
    widget.lists["codex/local"].setCurrentRow(0)
    widget.lists["github/local"].setCurrentRow(0)
    assert widget.current_selection == ("github_accounts", "g")
    assert not widget.lists["codex/local"].selectedItems()
    assert widget.details_host.isVisible()
    with qtbot.waitSignal(widget.action_requested) as signal:
        widget.request_action("login")
    assert signal.args == ["login", "github_accounts", "g"]


def test_responsive_plate_height_and_scroll(qtbot):
    widget = page(qtbot)
    qtbot.waitUntil(lambda: widget._columns == 2)
    assert widget.grid.getItemPosition(1)[:2] == (0, 1)
    assert widget.lists["codex/local"].height() < 100
    widget.resize(640, 520)
    qtbot.waitUntil(lambda: widget._columns == 1)
    assert widget.grid.getItemPosition(1)[:2] == (1, 0)
    qtbot.waitUntil(lambda: widget.scroll.verticalScrollBar().maximum() > 0)
    assert widget.scroll.horizontalScrollBar().maximum() == 0


def test_record_change_preserves_existing_selection_and_filter(qtbot):
    widget = page(qtbot)
    widget.select("github_accounts", "g")
    widget.environment.setCurrentIndex(widget.environment.findData("local"))
    widget.set_records(ACCOUNTS, [{**GITHUB[0], "login": "updated-login"}], ENVS, AUTH)
    assert widget.environment.currentData() == "local"
    assert widget.current_selection == ("github_accounts", "g")
    assert "updated-login" in widget.lists["github/local"].currentItem().text()


def test_programmatic_selection_notifies_once_and_keyboard_actions(qtbot):
    widget = page(qtbot)
    events = []
    widget.selected.connect(lambda kind, record_id: events.append((kind, record_id)))
    widget.select("github_accounts", "g")
    widget.select("github_accounts", "g")
    assert events == [("github_accounts", "g")]
    listing = widget.lists["github/local"]
    with qtbot.waitSignal(widget.action_requested) as signal:
        qtbot.keyClick(listing, Qt.Key.Key_Return)
    assert signal.args == ["edit", "github_accounts", "g"]
    with qtbot.waitSignal(widget.action_requested) as signal:
        qtbot.keyClick(listing, Qt.Key.Key_Delete)
    assert signal.args == ["delete", "github_accounts", "g"]


def test_each_tool_plate_holds_one_plate_per_environment_that_scrolls(qtbot):
    many = [{"id": f"r{i}", "name": f"遠端 {i}", "tool": "codex", "auth": "subscription", "environment_id": "ssh"} for i in range(6)]
    widget = page(qtbot, ACCOUNTS + many)
    assert [key for key in widget.lists if key.startswith("codex/")] == ["codex/local", "codex/ssh"]     # this PC first
    remote = widget.lists["codex/ssh"]
    assert remote.count() == 6 and "開發主機" not in remote.item(0).text()      # the plate names the environment
    assert "開發主機" in remote.item(0).toolTip()
    qtbot.waitUntil(lambda: remote.verticalScrollBar().maximum() > 0)          # more than four rows: it scrolls
    assert widget.lists["codex/local"].verticalScrollBar().maximum() == 0


def test_short_environment_lists_show_every_row_without_a_scrollbar_under_the_theme(qtbot):
    from vibe_launcher.theme import build_stylesheet
    two = [{"id": f"r{i}", "name": f"遠端 {i}", "tool": "codex", "auth": "subscription", "environment_id": "ssh"} for i in range(2)]
    widget = AccountsPage()
    qtbot.addWidget(widget)
    widget.setStyleSheet(build_stylesheet())          # the window's theme pads lists and rows
    widget.set_records(two, [], ENVS, AUTH)
    widget.resize(1180, 760)
    widget.show()
    listing = widget.lists["codex/ssh"]
    qtbot.wait(50)
    assert listing.verticalScrollBar().maximum() == 0


def test_right_click_selects_the_account_and_asks_the_window_for_its_actions(qtbot):
    widget = page(qtbot)
    asked = []
    widget.context_requested = lambda kind, record_id, where: asked.append((kind, record_id))
    listing = widget.lists["codex/local"]
    listing.customContextMenuRequested.emit(listing.visualItemRect(listing.item(0)).center())
    assert asked == [("accounts", "c")] and widget.current_selection == ("accounts", "c")
