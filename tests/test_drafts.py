import json
import time

from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QWidget

from vibe_launcher import runtime
from vibe_launcher.drafts import FormDraft, clear_draft, draft_path


def form(qtbot):
    host = QWidget()
    qtbot.addWidget(host)
    name, secret, choice, flag = QLineEdit(host), QLineEdit(host), QComboBox(host), QCheckBox(host)
    secret.setEchoMode(QLineEdit.EchoMode.Password)
    choice.addItem("A", "a")
    choice.addItem("B", "b")
    return host, {"name": name, "ssh_password": QLineEdit(host), "hidden": secret, "choice": choice, "flag": flag}


def test_draft_round_trip_skips_secrets(qtbot, tmp_path):
    host, fields = form(qtbot)
    draft = FormDraft(host, tmp_path, "k", fields)
    assert not draft.restored
    fields["name"].setText("工地")
    fields["ssh_password"].setText("pw")
    fields["hidden"].setText("pw2")
    fields["choice"].setCurrentIndex(1)
    fields["flag"].setChecked(True)
    draft.save()
    stored = draft_path(tmp_path, "k").read_text(encoding="utf-8")
    assert "pw" not in stored
    host2, again = form(qtbot)
    restored = FormDraft(host2, tmp_path, "k", again)
    assert restored.restored
    assert again["name"].text() == "工地" and again["choice"].currentData() == "b" and again["flag"].isChecked()
    assert again["ssh_password"].text() == "" and again["hidden"].text() == ""


def test_draft_saves_after_edits_and_clears(qtbot, tmp_path):
    host, fields = form(qtbot)
    draft = FormDraft(host, tmp_path, "k", fields)
    fields["name"].setText("x")
    qtbot.waitUntil(lambda: draft_path(tmp_path, "k").exists(), timeout=3000)
    fields["name"].setText("")
    draft.save()
    assert not draft_path(tmp_path, "k").exists()              # back to the start: nothing to keep
    fields["name"].setText("y")
    draft.save()
    clear_draft(tmp_path, "k")
    assert not draft_path(tmp_path, "k").exists()


def test_stale_or_broken_draft_is_ignored(qtbot, tmp_path):
    runtime.write_json(draft_path(tmp_path, "old"), {"saved": time.time() - 30 * 24 * 3600, "values": {"name": "old"}})
    host, fields = form(qtbot)
    assert not FormDraft(host, tmp_path, "old", fields).restored and fields["name"].text() == ""
    assert not draft_path(tmp_path, "old").exists()
    runtime.write_json(draft_path(tmp_path, "bad"), {"saved": "x", "values": []})
    assert not FormDraft(host, tmp_path, "bad", fields).restored


def test_record_dialog_keeps_input_until_the_record_is_saved(qtbot, tmp_path):
    from types import SimpleNamespace

    from vibe_launcher.ui import RecordDialog
    parent = QWidget()
    parent.store = SimpleNamespace(path=tmp_path / "store.sqlite3")
    qtbot.addWidget(parent)
    fields = [("name", "專案名稱", ""), ("path", "資料夾路徑", "")]
    dialog = RecordDialog("新增專案", fields, {}, None, parent, draft=True)
    dialog.fields["name"].setText("打到一半")
    dialog.reject()                                          # closed by accident (Esc) or a refused save
    again = RecordDialog("新增專案", fields, {}, None, parent, draft=True)
    assert again.fields["name"].text() == "打到一半" and again.draft.restored
    again._discard({})
    assert again.fields["name"].text() == ""
    third = RecordDialog("新增專案", fields, {}, None, parent, draft=True)
    assert not third.draft.restored
    plain = RecordDialog("新增專案", fields, {}, None, parent)
    assert plain.draft is None
