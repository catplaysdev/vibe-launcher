"""The quota plan editor and the terminal's quota and account controls.

- quota policy dialog: The quota plan editor: templates, wiring by mouse and keyboard, validation beside the blocks, saving and preview.
- ui quota
"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QLabel, QWidget
from vibe_launcher import runtime
from vibe_launcher.account_check import Finding, Report
from vibe_launcher.account_check_dialog import AccountCheckDialog
from vibe_launcher.quota_policy import (AUTO_CONTINUE, TEMPLATES, plan_store, Policy, PolicyStore, ProgressStore,
                                        template_plan)
from vibe_launcher.quota_policy_dialog import CATEGORY_ORDER, arrange, category, hue, QuotaPolicyDialog
from vibe_launcher.ui_quota import QuotaActions
from vibe_launcher.workspace_page import TabInfo, TerminalPane


# --- quota policy dialog ----------------------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def english(restore_controls):
    """These tests read English messages (the suite otherwise runs with the Traditional Chinese runtime catalog)."""
    from vibe_launcher import i18n, runtime as engine
    i18n.set_language("en")
    engine._MESSAGES = {}

ACCOUNTS = [{"id": key, "name": key.upper(), "tool": "codex", "auth": "subscription", "environment_id": "local"}
            for key in ("a", "b")]
REMOTE = {"id": "r", "name": "R", "tool": "codex", "auth": "subscription", "environment_id": "ssh"}
CLAUDE = {"id": "c", "name": "C", "tool": "claude", "auth": "subscription", "environment_id": "local"}


@pytest.fixture
def editor(qtbot, tmp_path):
    dialog = QuotaPolicyDialog(None, ACCOUNTS + [REMOTE, CLAUDE], "local", "codex", plan_store(tmp_path))
    qtbot.addWidget(dialog)
    return dialog


def edge(dialog, source, branch):
    return next((e["to"] for e in dialog.edges if e["from"] == source and e["branch"] == branch), None)


def test_a_new_library_starts_from_a_valid_template_with_the_defaults(editor, tmp_path):
    assert set(editor.names) == {"a", "b"}                       # only this environment and CLI
    assert not editor.issues and editor.threshold.value() == 10 and editor.interval.value() == 120
    assert [node["type"] for node in editor.nodes] == ["start", "condition", "select", "handoff", "wait", "keep"]
    assert plan_store(tmp_path).load() == {}                      # nothing is saved before the user saves
    assert editor.save()
    [plan] = plan_store(tmp_path).load().values()
    assert plan.revision == 1 and plan.plan_id == plan.id and runtime.flow_issues(plan.flow, plan.account_ids) == []


@pytest.mark.parametrize("template", TEMPLATES)
def test_each_template_selects_by_its_strategy(editor, template):
    editor.new_plan(template)
    select = editor.block("select")
    assert select["strategy"] == template and select["accounts"] == ["a", "b"]
    assert editor.block("empty")["type"] == "wait" and not editor.issues


def test_an_open_branch_is_marked_on_its_block_and_blocks_saving(editor, tmp_path):
    editor.select_block("select")
    editor.connect_branch("select", "none", None)
    assert editor.node_issues == {"select": ["unconnected"]}
    assert not editor.save_button.isEnabled() and not editor.apply_button.isEnabled()
    assert "Connect every branch" in editor.status.text()
    assert not editor.save() and plan_store(tmp_path).load() == {}
    editor.connect_branch("select", "none", "empty")
    assert not editor.issues and editor.save_button.isEnabled()


def test_loops_unguarded_switching_and_the_fixed_blocks_are_refused(editor):
    editor.connect_branch("start", "next", "select")
    assert {code for _, code in editor.issues} >= {"unguarded"}
    editor.connect_branch("start", "next", "low")
    editor.add_block("condition")
    added = editor.canvas.current
    assert editor.node_issues[added] == ["unconnected", "unconnected"] or "unreachable" in editor.node_issues[added]
    editor.connect_branch("low", "no", added)
    assert editor.link_issue(added, "yes", "low") == "cycle"
    assert not editor.connect_branch(added, "yes", "low")                  # a loop is refused before it exists
    assert edge(editor, added, "yes") is None and "loop back" in editor.status.text()
    for fixed in ("start", "handoff"):
        editor.select_block(fixed)
        editor.delete_block()
        assert editor.block(fixed) is not None


def test_keyboard_selects_moves_wires_and_deletes(editor, qtbot):
    editor.show()
    editor.canvas.setFocus()
    editor.select_block("start")
    qtbot.keyClick(editor.canvas, Qt.Key.Key_Down)
    assert editor.canvas.current == "low" and editor.block_list.currentItem().data(Qt.ItemDataRole.UserRole) == "low"
    x = editor.block("low")["x"]
    qtbot.keyClick(editor.canvas, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier)
    assert editor.block("low")["x"] == x + 20
    editor.add_block("notify")
    added = editor.canvas.current
    editor.select_block("low")
    no_box = editor.connect_boxes["no"]
    assert no_box.accessibleName() == "Connect No to"
    no_box.setCurrentIndex(no_box.findData(added))                 # the keyboard path to wiring
    assert edge(editor, "low", "no") == added
    assert editor.issues == [("keep", "unreachable")]           # the block it replaced is now marked
    editor.select_block(added)
    editor.canvas.setFocus()
    qtbot.keyClick(editor.canvas, Qt.Key.Key_Delete)
    assert editor.block(added) is None and edge(editor, "low", "no") is None


def test_a_branch_is_wired_by_dragging_its_port_onto_a_block(editor, qtbot):
    editor.show()
    qtbot.waitExposed(editor)
    editor.add_block("notify")
    added = editor.block(editor.canvas.current)
    start = editor.canvas.port(editor.block("low"), "no").toPoint()
    target = editor.canvas.rect_of(added).center().toPoint()
    qtbot.mousePress(editor.canvas, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(editor.canvas, target)
    qtbot.mouseRelease(editor.canvas, Qt.MouseButton.LeftButton, pos=target)
    assert edge(editor, "low", "no") == added["id"]


def test_connect_to_menu_lists_every_branch(editor):
    menu = editor.connect_menu(editor.block("select"))
    titles = [action.text() for action in menu.actions() if action.menu()]
    assert len(titles) == 2 and all("Connect" in title for title in titles)
    assert any(action.text() == "Delete block" for action in menu.actions())


def test_save_as_rename_delete_and_apply(editor, tmp_path, monkeypatch):
    assert editor.save()
    first = editor.plan_id
    editor.rename()
    assert editor.name.hasFocus() or editor.name.selectedText()
    editor.name.setText("Main")
    editor._changed()
    assert editor.save() and plan_store(tmp_path).load()[first].name == "Main"
    assert plan_store(tmp_path).load()[first].revision == 2         # an edit is a new revision ...
    assert editor.save() and plan_store(tmp_path).load()[first].revision == 2   # ... saving again is not
    editor.save_as()
    assert len(plan_store(tmp_path).load()) == 2 and editor.plan_id != first
    monkeypatch.setattr(editor, "_confirm", lambda *args: False)
    editor.delete_plan()
    assert len(plan_store(tmp_path).load()) == 2
    monkeypatch.setattr(editor, "_confirm", lambda *args: True)
    editor.delete_plan()
    assert set(plan_store(tmp_path).load()) == {first}
    editor.apply()
    assert editor.result_plan.id == first and editor.result() == editor.DialogCode.Accepted


@pytest.mark.parametrize("quota, outcome, target", [((5, 80), "handoff", "b"), ((50, 80), "keep", None),
                                                    ((5, 3), "wait", None), ((-1, 80), "unknown", None)])
def test_preview_uses_made_up_quota_and_starts_nothing(editor, tmp_path, quota, outcome, target):
    editor.preview_current.setCurrentIndex(editor.preview_current.findData("a"))
    for identifier, value in zip(("a", "b"), quota):
        editor.preview_quota[identifier].setValue(value)
    assert editor.preview() == (outcome, target)
    if outcome == "handoff":
        assert editor.canvas.trace == ["start", "low", "select", "handoff"]
    assert plan_store(tmp_path).load() == {} and not ProgressStore(tmp_path).load()


def test_the_editor_renders_with_its_blocks(editor, qtbot, tmp_path):
    editor.show()
    qtbot.waitExposed(editor)
    editor.connect_branch("select", "none", None)                  # one problem to draw beside its block
    image = editor.grab()
    assert not image.isNull() and image.save(str(tmp_path / "quota-plans.png"))
    rects = [editor.canvas.rect_of(node) for node in editor.nodes]
    assert all(not a.intersects(b) for i, a in enumerate(rects) for b in rects[i + 1:])


def test_auto_continue_waits_for_the_same_account_and_switches_nothing(editor, tmp_path):
    editor.new_plan(AUTO_CONTINUE)
    assert [node["type"] for node in editor.nodes] == ["start", "condition", "wait", "keep"] and not editor.issues
    assert editor.title_of(editor.block("continue")) == "Auto continue"
    assert editor.detail_of(editor.block("continue")) == "Continues after a verified reset"
    editor.preview_current.setCurrentIndex(editor.preview_current.findData("a"))
    editor.preview_quota["a"].setValue(0)
    editor.preview_quota["b"].setValue(80)                                  # another account would not be used
    assert editor.preview() == ("wait", None) and "continue the task automatically" in editor.status.text()
    editor.preview_quota["a"].setValue(5)
    assert editor.preview() == ("keep", None)
    assert editor.save()
    [plan] = plan_store(tmp_path).load().values()
    assert plan.name == "Auto continue (same account)" and not any(n["type"] == "handoff" for n in plan.flow["nodes"])


def test_claude_explains_its_own_way_of_continuing(qtbot, tmp_path):
    claude = [dict(account, tool="claude") for account in ACCOUNTS]
    dialog = QuotaPolicyDialog(None, claude, "local", "claude", plan_store(tmp_path))
    qtbot.addWidget(dialog)
    dialog.new_plan(AUTO_CONTINUE)
    assert dialog.detail_of(dialog.block("continue")) == "Claude continues by itself after the reset"
    assert "never restarts Claude" in dialog.explanation_of(dialog.block("continue"))


def test_the_safe_handoff_comes_and_goes_with_account_selection(editor):
    editor.new_plan(AUTO_CONTINUE)
    editor.select_block("exhausted")
    editor.add_block("select")
    first = editor.canvas.current
    [handoff] = [node for node in editor.nodes if node["type"] == "handoff"]
    assert edge(editor, first, "found") == handoff["id"]
    assert editor.block(first)["x"] > editor.block("exhausted")["x"]       # placed right of the selected block
    editor.add_block("select")
    second = editor.canvas.current
    assert edge(editor, second, "found") == handoff["id"]
    assert [node["type"] for node in editor.nodes].count("handoff") == 1
    editor.select_block(handoff["id"])
    editor.delete_block()
    assert editor.block(handoff["id"]) and "comes and goes" in editor.status.text()
    for ident in (first, second):
        editor.select_block(ident)
        editor.delete_block()
    assert not any(node["type"] == "handoff" for node in editor.nodes) and not editor.issues


def test_only_allowed_targets_are_offered_and_a_present_one_stays_visible(editor):
    editor.select_block("select")
    found = editor.connect_boxes["found"]
    assert [found.itemData(i) for i in range(found.count())] == ["", "handoff"]   # only the safe handoff
    none = [editor.connect_boxes["none"].itemData(i) for i in range(editor.connect_boxes["none"].count())]
    assert "handoff" not in none and "start" not in none and "select" not in none and "empty" in none
    assert not editor.connect_branch("low", "yes", "handoff")
    assert edge(editor, "low", "yes") == "select" and "Cannot connect" in editor.status.text()
    # A wire that is no longer allowed (here: one looping back) stays listed, marked, instead of hiding where it goes.
    editor.edges = [dict(e, to="low") if e["from"] == "select" and e["branch"] == "none" else e for e in editor.edges]
    editor._changed()
    editor.select_block("select")
    box = editor.connect_boxes["none"]
    assert box.currentData() == "low" and "(not allowed)" in box.currentText()
    menu = editor.connect_menu(editor.block("select"))
    none_menu = [action.menu() for action in menu.actions() if action.menu()][1]
    assert [action.isEnabled() for action in none_menu.actions() if action.text().endswith("low (not allowed)")] == [False]


def test_dragging_a_wire_highlights_allowed_blocks_and_refuses_the_others(editor, qtbot):
    editor.show()
    qtbot.waitExposed(editor)
    start = editor.canvas.port(editor.block("select"), "found").toPoint()
    qtbot.mousePress(editor.canvas, Qt.MouseButton.LeftButton, pos=start)
    assert editor.canvas.legal == {"handoff"} and "highlighted" in editor.status.text()
    target = editor.canvas.rect_of(editor.block("keep")).center().toPoint()
    qtbot.mouseMove(editor.canvas, target)
    assert not editor.canvas.grab().isNull()                               # paints the dimmed blocks
    qtbot.mouseRelease(editor.canvas, Qt.MouseButton.LeftButton, pos=target)
    assert edge(editor, "select", "found") == "handoff" and "Cannot connect" in editor.status.text()
    assert editor.canvas.legal == set()


def test_tidy_layout_goes_left_to_right_without_overlaps(editor):
    editor.add_block("notify")                                             # not wired: the band below
    added = editor.canvas.current
    for node in editor.nodes:
        node["x"] = node["y"] = 0                                          # a heap
    editor.tidy()
    spots = {node["id"]: (node["x"], node["y"]) for node in editor.nodes}
    rects = [editor.canvas.rect_of(node) for node in editor.nodes]
    assert all(not a.intersects(b) for i, a in enumerate(rects) for b in rects[i + 1:])
    assert all(spots[e["from"]][0] < spots[e["to"]][0] for e in editor.edges)
    assert spots["select"][1] < spots["keep"][1] and spots["handoff"][1] < spots["empty"][1]   # Yes above No
    assert spots[added][1] > max(y for ident, (_, y) in spots.items() if ident != added)
    assert arrange(editor.nodes, editor.edges) == spots                    # tidying again moves nothing


def test_moving_blocks_is_no_new_revision_and_a_click_changes_nothing(editor, tmp_path, qtbot):
    assert editor.save()
    plan_id = editor.plan_id
    editor.block("keep")["x"] += 40
    editor.tidy()
    assert editor.save() and plan_store(tmp_path).load()[plan_id].revision == 1   # terminals need no new apply
    editor.threshold.setValue(20)
    assert editor.save() and plan_store(tmp_path).load()[plan_id].revision == 2
    editor.show()
    qtbot.waitExposed(editor)
    point = editor.canvas.rect_of(editor.block("low")).center().toPoint()
    with qtbot.assertNotEmitted(editor.canvas.changed):
        qtbot.mouseClick(editor.canvas, Qt.MouseButton.LeftButton, pos=point)
    assert editor.canvas.current == "low"


def test_four_categories_each_with_its_own_colour():
    assert [category(kind) for kind in ("start", "condition", "select", "handoff", "wait", "notify", "keep")] == [
        "trigger", "check", "choose", "outcome", "outcome", "outcome", "outcome"]
    assert {category(kind) for kind in runtime.FLOW_KINDS} == set(CATEGORY_ORDER)
    assert len({hue(kind) for kind in runtime.FLOW_KINDS}) == 4


# --- the accounts a plan may switch between are checked before it is saved ---------------------------------------------
@pytest.fixture
def checked(qtbot, tmp_path):
    state = {"findings": [Finding("b", "settings", "skills/review"), Finding("b", "plugin", "pages", ("a",))],
             "calls": [], "aligned": []}

    def checker(accounts, reference):
        state["calls"].append(([account["id"] for account in accounts], reference))
        return Report(reference, tuple(account["id"] for account in accounts), list(state["findings"]))

    def aligner(target, reference):
        state["aligned"].append((target["id"], reference["id"]))
        return ["skills/review"]
    dialog = QuotaPolicyDialog(None, ACCOUNTS, "local", "codex", plan_store(tmp_path), own_account="a",
                               checker=checker, aligner=aligner)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog.check_state == "done", timeout=5000)
    return dialog, state


def test_differing_accounts_block_saving_until_each_is_resolved(checked, tmp_path, qtbot, monkeypatch):
    dialog, state = checked
    assert state["calls"] == [(["a", "b"], "a")]                          # the terminal's own account is the reference
    assert dialog.node_issues["select"] == ["accounts_differ"] and not dialog.save_button.isEnabled()
    assert not dialog.save() and plan_store(tmp_path).load() == {}
    assert "2 account differences" in dialog.status.text()
    monkeypatch.setattr(dialog, "_confirm_align", lambda *args: True)
    state["findings"] = [Finding("b", "plugin", "pages", ("a",))]          # left after aligning
    assert dialog.align_account("b") and state["aligned"] == [("b", "a")]
    qtbot.waitUntil(lambda: dialog.check_state == "done" and len(state["calls"]) == 2, timeout=5000)
    assert len(dialog.open_findings()) == 1 and not dialog.save_button.isEnabled()
    dialog.accept_gap("b", "plugin:pages", True)                           # switch to b without that plugin
    assert dialog.accounts_ready() and dialog.save_button.isEnabled() and dialog.save()
    [plan] = plan_store(tmp_path).load().values()
    assert plan.accepted_gaps == {"b": ["plugin:pages"]}


def test_an_account_leaves_the_plan_but_the_terminals_own_stays(checked, qtbot):
    dialog, state = checked
    assert not dialog.remove_account("a")
    assert dialog.remove_account("b") and dialog.block("select")["accounts"] == ["a"]
    qtbot.waitUntil(lambda: dialog.check_state == "single", timeout=5000)
    assert dialog.accounts_ready() and "nothing to compare" in dialog.account_status.text()


def test_choosing_another_reference_checks_again(checked, qtbot, monkeypatch):
    dialog, state = checked
    monkeypatch.setattr(dialog, "_confirm_align", lambda *args: False)
    dialog.set_reference("b")
    qtbot.waitUntil(lambda: len(state["calls"]) == 2 and dialog.check_state == "done", timeout=5000)
    assert state["calls"][-1] == (["a", "b"], "b")
    assert not dialog.align_account("a") and state["aligned"] == []      # nothing changes without a confirmation


def test_the_check_dialog_offers_the_fixes_for_each_kind(checked, qtbot):
    from PySide6.QtWidgets import QCheckBox, QPushButton
    dialog, state = checked
    view = AccountCheckDialog(dialog)
    qtbot.addWidget(view)
    buttons = [button.text() for button in view.findChildren(QPushButton) if button.isVisibleTo(view)]
    assert "Align to A" in buttons and "Remove from this plan" in buttons
    [box] = [box for box in view.findChildren(QCheckBox) if box.isVisibleTo(view)]
    assert "Plugin pages" in box.text() and "A has it" in box.text() and not box.isChecked()
    box.setChecked(True)
    assert dialog.accepted == {"b": {"plugin:pages"}}
    assert any(box.isChecked() for box in view.findChildren(QCheckBox) if box.isVisibleTo(view))


def test_an_answer_for_an_older_pool_is_ignored(checked, qtbot):
    dialog, state = checked
    dialog.check_state = "checking"
    dialog._checked(dialog._runner.generation - 1, Report("a", ("a", "b"), []), None)
    assert dialog.check_state == "checking"


def test_without_a_local_check_the_host_checks_at_each_handoff(editor):
    assert editor.check_state == "unavailable" and editor.accounts_ready()
    assert "SSH host" in editor.account_status.text() and not editor.check_button.isEnabled()


# --- ui quota ---------------------------------------------------------------------------------------------------------
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


def test_flow_editor_keeps_the_accepted_account_differences(qtbot, tmp_path):
    from vibe_launcher.quota_policy import plan_store, template_plan
    from vibe_launcher.quota_policy_dialog import QuotaPolicyDialog
    accounts = [{"id": key, "name": key.upper(), "tool": "codex", "auth": "subscription", "environment_id": "local"}
                for key in ("a", "b")]
    store = plan_store(tmp_path)
    store.save({"p1": template_plan("p1", "P", "priority", "local", ["a", "b"], accepted_gaps={"b": ["plugin:pages"]})})
    dialog = QuotaPolicyDialog(None, accounts, "local", "codex", store, "p1")
    qtbot.addWidget(dialog)
    assert dialog.current_plan().accepted_gaps == {"b": ["plugin:pages"]}


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


# --- a plan's accounts are checked again when it is applied and when their profiles change ------------------------------
def fake_checks(monkeypatch, findings, stamp):
    from vibe_launcher import account_check
    from vibe_launcher.account_check import Report
    calls = []

    def check(accounts, reference, root, project, refresh=True):
        calls.append(([account["id"] for account in accounts], reference, refresh))
        return Report(reference, tuple(account["id"] for account in accounts), list(findings), {"a": stamp[0]})
    monkeypatch.setattr(account_check, "check", check)
    monkeypatch.setattr(account_check, "signature", lambda accounts, root: {"a": stamp[0]})
    return calls


def test_a_saved_plan_is_applied_only_while_its_accounts_still_match(qtbot, tmp_path, monkeypatch):
    from vibe_launcher.account_check import Finding
    view = plan_view(qtbot, tmp_path)
    plan = template_plan("p1", "Main", "priority", "local", ["a", "b"])
    calls = fake_checks(monkeypatch, [Finding("b", "plugin", "pages", ("a",))], ["1"])
    view._check_then_apply("tab", plan)
    qtbot.waitUntil(lambda: any("was not applied" in text for text, _ in view.messages), timeout=5000)
    assert "Plugin pages" in view.messages[-1][0] and view.messages[-1][1] == "warn"
    assert PolicyStore(tmp_path).load() == {} and not view.calls       # nothing reached the terminal
    view._check_then_apply("tab", replace(plan, accepted_gaps={"b": ["plugin:pages"]}))
    qtbot.waitUntil(lambda: "session" in PolicyStore(tmp_path).load(), timeout=5000)
    assert calls == [(["a", "b"], "a", True)] * 2
    assert view.calls[-1][1]["operation"]["control_action"] == "policy"


def test_a_changed_profile_is_checked_again_and_new_differences_are_reported_once(qtbot, tmp_path, monkeypatch):
    from vibe_launcher.account_check import Finding
    view = plan_view(qtbot, tmp_path)
    findings, stamp = [], ["1"]
    calls = fake_checks(monkeypatch, findings, stamp)
    status = {"status": "idle", "account_id": "a",
              "policy": template_plan("session", "Main", "priority", "local", ["a", "b"]).to_dict()}

    def poll():
        view._quota_control_result("tab", dict(status), {"control_action": "status", "quiet": True})

    def settled(count):
        qtbot.waitUntil(lambda: len(calls) == count and not view._plan_checks["tab"].get("running"), timeout=5000)

    poll()
    settled(1)                                        # the first look after the plan runs (a restore, a reopen)
    assert calls[0] == (["a", "b"], "a", False) and not view.messages
    poll()
    assert len(calls) == 1                            # nothing changed in the profiles: nothing is checked
    findings.append(Finding("b", "plugin", "slides", ("a",)))
    stamp[0] = "2"                                    # a plugin was installed on a meanwhile
    poll()
    settled(2)
    warned = [text for text, tone in view.messages if "differ now" in text]
    assert len(warned) == 1 and "slides" in warned[0]
    stamp[0] = "3"
    poll()
    settled(3)
    assert len([text for text, _ in view.messages if "differ now" in text]) == 1     # said once
