import pytest
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QLabel, QWidget

from vibe_launcher.ui_quota import QuotaActions
from dataclasses import replace

from vibe_launcher.quota_policy import Policy, PolicyStore, ProgressStore, plan_store, template_plan
from vibe_launcher.workspace_page import TabInfo, TerminalPane


@pytest.fixture(autouse=True)
def english(restore_controls):
    """These tests read English messages (the suite otherwise runs with the Traditional Chinese runtime catalog)."""
    from vibe_launcher import i18n, runtime as engine
    i18n.set_language("en")
    engine._MESSAGES = {}


class View(QuotaActions, QWidget):
    def __init__(self):
        super().__init__()
        self.info = TabInfo("tab", "Work", tool="codex", kind="agent", session_id="session", account_id="a", environment_id="local")
        self.tab = SimpleNamespace(info=self.info, pane=SimpleNamespace(title=QLabel()))
        self.workspace = SimpleNamespace(_tabs={"tab": self.tab}, shutting_down=False)
        self._records = {"accounts": [{"id": "a", "name": "A", "tool": "codex", "auth": "subscription", "environment_id": "local"},
                                     {"id": "b", "name": "B", "tool": "codex", "auth": "subscription", "environment_id": "local"},
                                     {"id": "remote", "tool": "codex", "auth": "subscription", "environment_id": "ssh"}]}
        self.calls = []
        self.messages = []

    def _start_operation(self, action, kwargs, callback, **options):
        self.calls.append((action, kwargs, callback))

    def _by_id(self, kind, identifier):
        return next((a for a in self._records[kind] if a["id"] == identifier), {})

    def _toast(self, message, tone="info"):
        self.messages.append((message, tone))


def test_terminal_toolbar_signals_are_keyboard_accessible(qtbot):
    info = TabInfo("tab", "Work", tool="codex", session_id="session")
    pane = TerminalPane(info, QWidget())
    qtbot.addWidget(pane)
    with qtbot.waitSignal(pane.quota_policy_requested) as signal:
        pane.quota_button.click()
    assert signal.args == ["tab", pane.quota_button]
    with qtbot.waitSignal(pane.account_switch_requested) as signal:
        pane.account_button.click()
    assert signal.args[0] == "tab"
    pane.show()
    assert not pane.grab().isNull()


def test_control_request_filters_private_ui_fields_and_updates_verified_status(qtbot):
    view = View()
    qtbot.addWidget(view)
    view._quota_control("tab", {"control_action": "status", "quiet": True})
    action, kwargs, callback = view.calls[-1]
    assert action == "codex-control"
    assert kwargs["session_id"] == "session"
    assert kwargs["operation"] == {"control_action": "status"}
    callback({"status": "idle", "account_id": "b", "thread_id": "thread"})
    assert view.info.account_id == "b"
    assert view.info.resume_thread_id == "thread"
    # A quiet check stays quiet unless something happened: here the flow moved the terminal to another account.
    assert [tone for _, tone in view.messages] == ["info"] and "B" in view.messages[0][0]
    callback({"status": "idle", "account_id": "b", "thread_id": "thread"})
    assert len(view.messages) == 1
    assert [a["id"] for a in view._quota_accounts("local")] == ["a", "b"]


def test_pending_switch_polls_only_while_terminal_exists(qtbot):
    view = View()
    qtbot.addWidget(view)
    view._quota_control_result("tab", {"status": "queued", "account_id": "a"},
                               {"control_action": "switch", "target_account_id": "b"})
    assert view._quota_control_timers["tab"].isActive()
    view.workspace._tabs.clear()
    view._quota_control_timers["tab"].timeout.emit()
    assert not view.calls
    assert "tab" not in view._quota_control_timers


def plan_view(qtbot, tmp_path):
    view = View()
    qtbot.addWidget(view)
    view.store = SimpleNamespace(path=tmp_path / "launcher.db")
    view._quota_report = lambda *_: {}
    view._error = lambda *_: (_ for _ in ()).throw(AssertionError("unexpected error"))
    return view


def menu_texts(view):
    view.edit_terminal_quota("tab")
    return [item.action.text for item in view._quota_menu.items]


def test_a_terminal_has_no_plan_until_one_is_applied(qtbot, tmp_path):
    view = plan_view(qtbot, tmp_path)
    plans = plan_store(tmp_path)
    plans.save({"p1": template_plan("p1", "Main", "round-robin", "local", ["b"])})
    texts = menu_texts(view)
    assert texts[0].startswith("None") and "Main" in texts and texts[-1].startswith("Edit plans")
    assert not view.calls and PolicyStore(tmp_path).load() == {}


def test_applying_a_plan_copies_it_and_later_edits_need_applying_again(qtbot, tmp_path):
    view = plan_view(qtbot, tmp_path)
    plans = plan_store(tmp_path)
    plan = template_plan("p1", "Main", "round-robin", "local", ["b"])
    plans.save({"p1": plan})
    view._apply_quota_plan("tab", plan)
    copy = PolicyStore(tmp_path).load()["session"]
    assert copy.plan_id == "p1" and copy.revision == 1 and copy.account_ids == ("b", "a")     # its own account watched
    action, kwargs, _ = view.calls[-1]
    assert action == "codex-control" and kwargs["operation"]["policy"]["id"] == "session"
    assert kwargs["operation"]["accounts"] == ["b", "a"]        # only ids leave the view
    plans.save({"p1": replace(plan, name="Main", revision=2, threshold=20.0)})
    assert PolicyStore(tmp_path).load()["session"].threshold == 10.0      # the terminal keeps its copy
    assert "Apply again: Main (edited since)" in menu_texts(view)
    view._apply_quota_plan("tab", None)
    assert PolicyStore(tmp_path).load() == {}
    assert view.calls[-1][1]["operation"] == {"control_action": "policy", "policy": None, "accounts": []}


def test_plans_of_another_environment_or_cli_are_not_offered(qtbot, tmp_path):
    view = plan_view(qtbot, tmp_path)
    view._records["accounts"].append({"id": "c1", "name": "C", "tool": "claude", "auth": "subscription", "environment_id": "local"})
    plan_store(tmp_path).save({"r": template_plan("r", "Remote", "priority", "ssh", ["remote"]),
                               "c": template_plan("c", "Claude", "priority", "local", ["c1"])})
    assert not [text for text in menu_texts(view) if text in ("Remote", "Claude")]


def test_round_robin_progress_is_kept_for_an_exact_reopen(qtbot, tmp_path):
    view = plan_view(qtbot, tmp_path)
    plan = template_plan("p1", "Main", "round-robin", "local", ["a", "b"])
    view._apply_quota_plan("tab", plan)
    view._quota_control_result("tab", {"status": "idle", "account_id": "a", "cursor": "b"}, {"control_action": "status", "quiet": True})
    assert ProgressStore(tmp_path).cursor("session") == "b"
    assert view._quota_launch_options("session", "local")["quota_cursor"] == "b"
    view._carry_quota_policy("session", "next")
    assert ProgressStore(tmp_path).cursor("next") == "b" and ProgressStore(tmp_path).cursor("session") is None


@pytest.mark.parametrize("accept", [True, False])
def test_different_approval_rules_are_aligned_only_on_request(qtbot, accept):
    view = View()
    qtbot.addWidget(view)
    asked = []
    view._confirm = lambda title, message, **kwargs: asked.append((title, message, kwargs)) or accept
    view._quota_control_result("tab", {"status": "queued", "account_id": "a"},
                               {"control_action": "switch", "target_account_id": "b"})
    view._quota_control_result("tab", {"status": "idle", "account_id": "a", "blocked": {"target": "b", "gaps": [
        {"kind": "rules", "name": "default.rules"}]}}, {"control_action": "status", "quiet": True})
    assert len(asked) == 1 and "default.rules" in asked[0][1] and asked[0][2]["confirm_text"] == "Align with the current rules"
    if accept:
        assert view.calls[-1][1]["operation"] == {"control_action": "switch", "target_account_id": "b", "accounts": ["b"],
                                                  "align_rules": True}
    else:
        assert not view.calls


def test_policy_restore_follows_new_session_and_attention_stops_switch(qtbot, tmp_path):
    view = View()
    qtbot.addWidget(view)
    view.store = SimpleNamespace(path=tmp_path / "launcher.db")
    store = PolicyStore(tmp_path)
    store.save({"old": Policy("old", "Flow", "local", ("a", "b"))})
    view._carry_quota_policy("old", "session")
    assert set(store.load()) == {"session"} and store.load()["session"].account_ids == ("a", "b")
    view._quota_control_result("tab", {"status": "queued", "account_id": "a"},
                               {"control_action": "switch", "target_account_id": "b"})
    view._quota_control_result("tab", {"status": "needs_attention", "account_id": "a"}, {"control_action": "status", "quiet": True})
    assert "tab" not in view._quota_control_polling
    assert "tab" not in view._quota_control_timers
    assert view.messages[-1][1] == "warn"


def test_reopen_options_come_from_the_local_worker_record_and_saved_flow(qtbot, tmp_path):
    from vibe_launcher import runtime
    view = View()
    qtbot.addWidget(view)
    view.store = SimpleNamespace(path=tmp_path / "launcher.db")
    folder = runtime.codex_control_path(tmp_path, "session")
    runtime.write_json(folder / "state.json", {"status": "closed", "environment_id": "local", "account_id": "b",
                                               "thread_id": "thread-1", "policy": None})
    PolicyStore(tmp_path).save({"session": Policy("session", "Flow", "local", ("a", "b"))})
    options = view._quota_launch_options("session", "local")
    assert options["resume_thread_id"] == "thread-1" and options["account_id"] == "b"
    assert options["quota_policy"]["account_ids"] == ["a", "b"]
    runtime.write_json(folder / "state.json", {"status": "closed", "environment_id": "local", "account_id": "remote",
                                               "thread_id": "../bad"})
    assert view._quota_launch_options("session", "local") == {"quota_policy": options["quota_policy"]}
    # A remote terminal's record lives on its host: only the saved flow comes back from here.
    assert set(view._quota_launch_options("session", "ssh")) == set()
    # An account deleted since the flow was saved leaves the pool; a pool with nothing left is not brought back.
    PolicyStore(tmp_path).save({"session": Policy("session", "Flow", "local", ("gone", "b"))})
    assert view._quota_launch_options("session", "local")["quota_policy"]["account_ids"] == ["b"]
    PolicyStore(tmp_path).save({"session": Policy("session", "Flow", "local", ("gone",))})
    assert "quota_policy" not in view._quota_launch_options("session", "local")


def test_saved_flow_is_watched_again_and_closed_terminals_are_not(qtbot, tmp_path):
    view = View()
    qtbot.addWidget(view)
    view.store = SimpleNamespace(path=tmp_path / "launcher.db")
    view._sync_quota_controls()
    assert not view._quota_control_timers
    PolicyStore(tmp_path).save({"session": Policy("session", "Flow", "local", ("a", "b"))})
    view._sync_quota_controls()
    assert view._quota_control_timers["tab"].isActive()
    view._quota_control_timers["tab"].timeout.emit()
    assert view.calls[-1][1]["operation"] == {"control_action": "status"}
    view._quota_control_result("tab", {"status": "idle", "policy": {"enabled": True, "interval": 60}}, {"control_action": "status", "quiet": True})
    assert view._quota_control_timers["tab"].interval() == 60000
    view._quota_control_result("tab", {"status": "needs_attention", "worker_alive": False,
                                       "policy": {"enabled": True, "interval": 60}}, {"control_action": "status", "quiet": True})
    assert "tab" not in view._quota_control_timers


def test_cancel_forgets_the_saved_flow_and_rejection_warns(qtbot, tmp_path):
    view = View()
    qtbot.addWidget(view)
    view.store = SimpleNamespace(path=tmp_path / "launcher.db")
    PolicyStore(tmp_path).save({"session": Policy("session", "Flow", "local", ("a", "b"))})
    view._quota_control_result("tab", {"status": "idle", "accepted": True}, {"control_action": "cancel"})
    assert PolicyStore(tmp_path).load() == {}
    view._quota_control_result("tab", {"status": "idle", "accepted": False, "message": "Control request rejected; verify its account pool and settings."},
                               {"control_action": "switch", "target_account_id": "b"})
    assert view.messages[-1][1] == "warn" and "tab" not in view._quota_control_polling


def test_codex_slots_remember_their_session(qtbot):
    from vibe_launcher.prefs import valid_desktop
    from vibe_launcher.store import Store
    slot = {"preset_id": "p", "kind": "agent", "account_id": "a", "session_id": "0123abcd"}
    assert valid_desktop({"slots": [slot]})["slots"] == [slot]
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder) / "launcher.db")
        saved = store.save("workspaces", {"id": "w", "name": "W", "slots": [slot]})
        assert saved["slots"] == [slot]


@pytest.mark.parametrize("accept", [True, False])
def test_account_differences_are_shown_and_the_user_decides(qtbot, accept):
    view = View()
    qtbot.addWidget(view)
    asked = []
    view._confirm = lambda title, message, **kwargs: asked.append(message) or accept
    view._quota_control_result("tab", {"status": "queued", "account_id": "a"},
                               {"control_action": "switch", "target_account_id": "b"})
    view._quota_control_result("tab", {"status": "idle", "account_id": "a", "blocked": {"target": "b", "gaps": [
        {"kind": "plugin", "name": "pages"}, {"kind": "app", "name": "ChatGPT Space"}]}},
        {"control_action": "status", "quiet": True})
    assert len(asked) == 1 and "pages" in asked[0] and "ChatGPT Space" in asked[0]
    assert "tab" not in view._quota_control_polling
    if accept:
        assert view.calls[-1][1]["operation"] == {"control_action": "switch", "target_account_id": "b", "accounts": ["b"],
                                                  "allow_gaps": True}
    else:
        assert not view.calls


def test_flow_editor_keeps_the_account_difference_choice(qtbot):
    from vibe_launcher.quota_policy_dialog import QuotaPolicyDialog
    accounts = [{"id": "a", "name": "A", "tool": "codex", "auth": "subscription", "environment_id": "local"}]
    dialog = QuotaPolicyDialog(None, accounts, "local", "codex")
    qtbot.addWidget(dialog)
    dialog.on_gaps.setCurrentIndex(dialog.on_gaps.findData("allow"))
    assert dialog.current_plan().on_gaps == "allow"
    dialog.on_gaps.setCurrentIndex(dialog.on_gaps.findData("block"))
    assert dialog.current_plan().on_gaps == "block"


@pytest.mark.parametrize("tool, visible", [("codex", True), ("claude", True), ("shell", False)])
def test_account_buttons_appear_on_codex_and_claude_terminals(qtbot, tool, visible):
    info = TabInfo("tab", "Work", tool=tool, kind="agent", session_id="session")
    pane = TerminalPane(info, QWidget())
    qtbot.addWidget(pane)
    assert pane.quota_button.isVisibleTo(pane) is visible and pane.account_button.isVisibleTo(pane) is visible


def test_claude_terminals_offer_only_claude_accounts(qtbot):
    view = View()
    qtbot.addWidget(view)
    view._records["accounts"] += [{"id": "c1", "name": "Account A", "tool": "claude", "auth": "subscription", "environment_id": "local"},
                                  {"id": "c2", "name": "API", "tool": "claude", "auth": "api", "model": "m", "environment_id": "local"}]
    assert [a["id"] for a in view._quota_accounts("local", "claude")] == ["c1"]
    assert [a["id"] for a in view._quota_accounts("local")] == ["a", "b"]
