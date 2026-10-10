"""Project icons (glyph / picture / recoloured SVG), service colours in Quick open, and the project page drawer."""
import json

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QDialog

from vibe_launcher import project_icons, theme
from vibe_launcher.project_icon_dialog import ProjectIconDialog
from vibe_launcher.store import Store
from vibe_launcher.ui import MainWindow
from test_workspace import FakeTerminal, SpawnService

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#000"/></svg>'


@pytest.fixture
def view(tmp_path, qtbot):
    store = Store(tmp_path / "launcher.db")
    store.save("presets", {"id": "p1", "name": "Site analysis", "environment_id": "local", "path": str(tmp_path),
                           "last_used": "2026-10-02T00:00:00+00:00"})
    store.save("presets", {"id": "p2", "name": "Frontend", "environment_id": "local", "path": str(tmp_path)})
    window = MainWindow(store, SpawnService(), terminal_factory=FakeTerminal)
    qtbot.addWidget(window, before_close_func=lambda w: w.workspace.shutdown())
    window.show()
    window.show_page("projects")
    yield window
    project_icons.configure(tmp_path, {})


def choose(monkeypatch, act):
    """Run the icon dialog with ``act(dialog)`` instead of user clicks; returns what exec answers."""
    def fake_exec(dialog):
        return act(dialog)
    monkeypatch.setattr(ProjectIconDialog, "exec", fake_exec)


def select(view, project_id):
    for row in range(view.project_list.count()):
        if view.project_list.item(row).data(0x0100) == project_id:
            view.project_list.setCurrentRow(row)
    assert view._selected_project == project_id


def test_entries_are_validated():
    assert project_icons.valid_entry({"kind": "glyph", "name": "cpu", "color": "#abcdef"}) == \
        {"kind": "glyph", "name": "cpu", "color": "#ABCDEF"}
    assert project_icons.valid_entry({"kind": "glyph", "name": "../x"}) is None
    assert project_icons.valid_entry({"kind": "file", "name": "../../evil.svg"}) is None
    assert project_icons.valid_entry({"kind": "file", "name": "a" * 32 + ".exe"}) is None
    assert project_icons.valid_entries({"p": {"kind": "glyph", "name": "star"}, "q": "bad", 3: {}}) == \
        {"p": {"kind": "glyph", "name": "star", "color": ""}}


def test_svg_recolour_keeps_none_and_paints_unfilled_shapes(qapp):
    text = project_icons.recolor_svg('<svg viewBox="0 0 1 1"><path fill="none" stroke="#123456"/>'
                                     '<path style="fill:red;opacity:1"/><path/></svg>', "#FF00AA")
    assert 'fill="none"' in text and 'stroke="#FF00AA"' in text and "fill:#FF00AA" in text
    assert text.startswith('<svg fill="#FF00AA"')
    picture = project_icons.render(SVG, ".svg", 20, 1.0, "#FF0000").toImage()
    assert picture.pixelColor(10, 10).name() == "#ff0000"


def test_pictures_are_checked_and_copied_privately(tmp_path, qapp):
    with pytest.raises(ValueError):
        project_icons.check_picture(SVG, ".exe")
    with pytest.raises(ValueError):
        project_icons.check_picture(b"not a picture", ".png")
    with pytest.raises(ValueError):
        project_icons.check_picture(b"x" * (project_icons.MAX_BYTES + 1), ".svg")
    name = project_icons.store_picture(tmp_path, SVG, ".svg")
    stored = tmp_path / "project-icons" / name
    assert stored.read_bytes() == SVG and project_icons.valid_entry({"kind": "file", "name": name})
    project_icons.remove_picture(tmp_path, name)
    assert not stored.exists()
    project_icons.remove_picture(tmp_path, "../launcher.db")       # never outside the icon folder


def test_choose_svg_with_colour_then_glyph_then_default(view, tmp_path, monkeypatch):
    source = tmp_path / "logo.svg"
    source.write_bytes(SVG)
    select(view, "p1")

    def pick_svg(dialog):
        dialog._pick_file = lambda: str(source)
        dialog.pick()
        dialog.choose_colour("#F472B6")
        return QDialog.DialogCode.Accepted
    choose(monkeypatch, pick_svg)
    view.change_project_icon()
    entry = view.prefs.project_icons["p1"]
    assert entry["kind"] == "file" and entry["color"] == "#F472B6"
    stored = tmp_path / "project-icons" / entry["name"]
    assert stored.exists()
    assert json.loads(view.prefs.path.read_text(encoding="utf-8"))["project_icons"]["p1"] == entry
    assert view.project_list.currentItem().data(0x0100 + 5) == project_icons.signature("p1")
    tile = view.project_tile.grab().toImage()
    colours = {tile.pixelColor(x, y).name() for x in range(tile.width()) for y in range(tile.height())}
    assert "#f472b6" in colours                                    # the recoloured SVG is what the header shows

    choose(monkeypatch, lambda d: (d.choose_glyph("cpu"), QDialog.DialogCode.Accepted)[1])
    view.change_project_icon()
    assert view.prefs.project_icons["p1"] == {"kind": "glyph", "name": "cpu", "color": "#F472B6"}
    assert not stored.exists()                                     # the replaced picture is deleted

    choose(monkeypatch, lambda d: (d.reset(), QDialog.DialogCode.Accepted)[1])
    view.change_project_icon()
    assert "p1" not in view.prefs.project_icons


def test_cancel_changes_nothing_and_photos_take_no_colour(view, tmp_path, monkeypatch):
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("#00FF00"))
    photo = tmp_path / "photo.png"
    image.save(str(photo))
    select(view, "p2")

    def pick_then_cancel(dialog):
        dialog._pick_file = lambda: str(photo)
        dialog.pick()
        dialog.choose_colour("#FF0000")              # ignored: a photo keeps its colours
        assert dialog.entry["color"] == "" and not dialog.default_swatch.isEnabled()
        return QDialog.DialogCode.Rejected
    choose(monkeypatch, pick_then_cancel)
    view.change_project_icon()
    assert "p2" not in view.prefs.project_icons and not (tmp_path / "project-icons").exists()


def test_bad_picture_is_refused_inside_the_dialog(view, tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a picture")
    dialog = ProjectIconDialog(view, "Frontend", None)
    dialog._pick_file = lambda: str(bad)
    dialog.pick()
    assert dialog.pending is None and dialog.error.isVisibleTo(dialog) and dialog.error.text()
    dialog.reject()


def test_deleted_project_forgets_its_icon_and_picture(view, tmp_path):
    name = project_icons.store_picture(tmp_path, SVG, ".svg")
    view.prefs.project_icons["p2"] = {"kind": "file", "name": name, "color": ""}
    view.store.delete("presets", "p2")
    view.reload()
    assert "p2" not in view.prefs.project_icons and not (tmp_path / "project-icons" / name).exists()


def test_home_shows_the_project_icon_not_the_account_tool(view):
    view.prefs.project_icons["p1"] = {"kind": "glyph", "name": "cpu", "color": ""}
    view._refresh_project_icons()
    tiles = [t for t in view.home_page.findChildren(type(view.project_tile).__mro__[1]) if t._project]
    assert {t._project for t in tiles} >= {"p1"}


def test_service_colours_are_their_own(qapp):
    for theme_id in theme.THEMES:
        theme.apply_theme(theme_id)
        claude, terminal = theme.resolve(theme.tool_hue("claude")), theme.resolve(theme.tool_hue("shell"))
        assert claude not in {theme.C.warning, theme.C.accent} and terminal not in {theme.C.warning, theme.C.accent}
        assert theme.contrast(claude, theme.C.surface) >= 3 and theme.contrast(terminal, theme.C.surface) >= 3
    theme.apply_theme(theme.DEFAULT_THEME)


def test_drawer_is_a_plain_section(view):
    from vibe_launcher.kit import IconButton
    from vibe_launcher.work_drawer import WorkDrawer
    assert "paintEvent" not in WorkDrawer.__dict__                 # no tinted frame around the cards
    assert isinstance(view.work_drawer.add, IconButton) and view.work_drawer.add.accessibleName()


def test_clicking_the_icon_in_the_project_list_opens_the_picker(view, monkeypatch, qtbot):
    from PySide6.QtCore import QPoint, Qt
    opened = []
    choose(monkeypatch, lambda d: (opened.append(view._selected_project), QDialog.DialogCode.Rejected)[1])
    listing = view.project_list
    row = next(r for r in range(listing.count()) if listing.item(r).data(0x0100) == "p2")
    rect = listing.visualItemRect(listing.item(row))
    qtbot.mouseClick(listing.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(rect.left() + 28, rect.center().y()))
    qtbot.waitUntil(lambda: opened == ["p2"], timeout=2000)                    # the icon: picker opens for that row
    opened.clear()
    qtbot.mouseClick(listing.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(rect.left() + 120, rect.center().y()))
    qtbot.wait(100)
    assert opened == []                                                         # the rest of the row only selects


def test_workspace_tiles_use_their_own_default_glyph_and_hue(qapp):
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QPainter

    def tile(**kwargs):
        image = QImage(64, 64, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        project_icons.draw(painter, QRectF(0, 0, 64, 64), entry={}, dpr=1.0, **kwargs)
        painter.end()
        return image
    assert tile(glyph="briefcase", hue="info") != tile()
    assert not project_icons.pixmap(None, 18, hue="info", glyph="briefcase").isNull()


def test_the_name_field_refuses_empty_and_taken_names(view, qtbot):
    dialog = ProjectIconDialog(view, "Site", None, title="Workspace", hue="info", glyph="briefcase",
                               name_label="Name", taken={"Data"})
    qtbot.addWidget(dialog)
    dialog.name_field.setText("  ")
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted and dialog.error.isVisibleTo(dialog)
    dialog.name_field.setText("data")
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    dialog.name_field.setText("  Site   work ")
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted and dialog.result_name() == "Site work"