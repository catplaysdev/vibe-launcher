"""The quota plan editor: templates, wiring by mouse and keyboard, validation beside the blocks, saving and preview."""
import pytest
from PySide6.QtCore import QPoint, Qt

from vibe_launcher import runtime
from vibe_launcher.quota_policy import AUTO_CONTINUE, TEMPLATES, ProgressStore, plan_store
from vibe_launcher.account_check import Finding, Report
from vibe_launcher.account_check_dialog import AccountCheckDialog
from vibe_launcher.quota_policy_dialog import CATEGORY_ORDER, QuotaPolicyDialog, arrange, category, hue


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
