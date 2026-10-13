"""The quota plan editor: templates, wiring by mouse and keyboard, validation beside the blocks, saving and preview."""
import pytest
from PySide6.QtCore import QPoint, Qt

from vibe_launcher import runtime
from vibe_launcher.quota_policy import TEMPLATES, ProgressStore, plan_store
from vibe_launcher.quota_policy_dialog import QuotaPolicyDialog


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
    editor.connect_branch(added, "yes", "low")
    editor.connect_branch(added, "no", "keep")
    assert [code for _, code in editor.issues] == ["cycle"]
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
