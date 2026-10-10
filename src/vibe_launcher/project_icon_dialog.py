"""Choose a project's icon: a built-in glyph or the user's own picture / SVG, with a colour for glyphs and SVGs.

Workspaces use the same dialog with their own title, hue and default glyph, plus a name field.

Nothing is written until Save: a picked picture is only checked and previewed; Save copies it into the launcher's data
folder (``project_icons.store_picture``). Cancel leaves the old icon and files as they were.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QButtonGroup, QColorDialog, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout, QWidget)

from . import project_icons
from .appearance import Swatch
from .i18n import tr
from .kit import FlowLayout, label, make_button, set_tone
from .theme import ACCENT_PRESETS, C, R, S, resolve, section_hue


class _Preview(QWidget):
    def __init__(self, dialog: "ProjectIconDialog"):
        super().__init__(dialog)
        self.dialog = dialog
        self.setFixedSize(72, 72)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        project_icons.draw(p, QRectF(self.rect()), entry=self.dialog.entry or {}, preview=self.dialog.pending,
                           dpr=max(2.0, self.devicePixelRatioF()), radius=R.lg, hue=self.dialog.hue, glyph=self.dialog.glyph)
        p.end()


class ProjectIconDialog(QDialog):
    def __init__(self, parent: QWidget, project_name: str, entry: dict | None, *, title: str = "", hue: str = "",
                 glyph: str = "folder", name_label: str = "", taken: frozenset[str] | set[str] = frozenset()):
        """``name_label`` turns the name into an editable field; Save then refuses an empty name or one in ``taken``."""
        super().__init__(parent)
        title = title or tr("Project icon")
        self.setWindowTitle(title)
        self.setModal(True)
        self.hue, self.glyph = hue or section_hue("projects"), glyph
        self.taken = {name.casefold() for name in taken}
        self.entry: dict | None = dict(entry) if entry else None
        self.pending: tuple[bytes, str] | None = None      # a picked picture, stored on Save
        root = QVBoxLayout(self)
        root.setContentsMargins(S.xl, S.xl, S.xl, S.lg)
        root.setSpacing(S.md)
        root.addWidget(label(title, "dialogTitle"))
        head = QHBoxLayout()
        head.setSpacing(S.lg)
        self.preview = _Preview(self)
        head.addWidget(self.preview)
        self.name_field: QLineEdit | None = None
        if name_label:
            column = QVBoxLayout()
            column.setSpacing(S.xs)
            column.addWidget(label(name_label, "subsection"))
            self.name_field = QLineEdit(project_name)
            self.name_field.setMaxLength(64)
            self.name_field.setAccessibleName(name_label)
            column.addWidget(self.name_field)
            head.addLayout(column, 1)
        else:
            name = label(project_name, "subsection", wrap=True)
            name.setTextFormat(Qt.TextFormat.PlainText)
            head.addWidget(name, 1)
        root.addLayout(head)

        root.addWidget(label(tr("Built-in icons"), "subsection"))
        glyphs_host = QWidget()
        glyphs = FlowLayout(glyphs_host, S.xs, S.xs)
        glyphs.setContentsMargins(0, 0, 0, 0)
        self.glyph_group = QButtonGroup(self)
        self.glyph_buttons: dict[str, QPushButton] = {}
        for glyph in project_icons.GLYPHS:
            button = make_button("", "subtle", glyph, tip=glyph)
            button.setAccessibleName(tr("Icon: {v0}", v0=glyph))
            button.setCheckable(True)
            button.setFixedSize(40, 36)
            button.clicked.connect(lambda _=False, g=glyph: self.choose_glyph(g))
            self.glyph_group.addButton(button)
            self.glyph_buttons[glyph] = button
            glyphs.addWidget(button)
        root.addWidget(glyphs_host)

        pick_row = QHBoxLayout()
        pick_row.addWidget(make_button(tr("Choose picture or SVG…"), "secondary", "upload", callback=lambda *_: self.pick()))
        pick_row.addStretch(1)
        root.addLayout(pick_row)
        root.addWidget(label(tr("SVG, PNG, JPG or WebP up to 1 MB. The file is copied into the launcher's data folder."),
                             "meta", wrap=True))

        root.addWidget(label(tr("Colour"), "subsection"))
        colours_host = QWidget()
        colours = FlowLayout(colours_host, S.xs, S.xs)
        colours.setContentsMargins(0, 0, 0, 0)
        self.colour_group = QButtonGroup(self)
        self.default_swatch = Swatch(resolve(self.hue), tr("Theme colour"))
        self.default_swatch.clicked.connect(lambda: self.choose_colour(""))
        self.colour_group.addButton(self.default_swatch)
        colours.addWidget(self.default_swatch)
        self.swatches: dict[str, Swatch] = {}
        for colour in ("#181818", "#FFFFFF", *ACCENT_PRESETS):
            swatch = Swatch(colour, colour)
            swatch.clicked.connect(lambda _=False, c=colour: self.choose_colour(c))
            self.colour_group.addButton(swatch)
            self.swatches[colour.upper()] = swatch
            colours.addWidget(swatch)
        self.custom_swatch = Swatch(C.accent, tr("Custom colour"))
        self.custom_swatch.clicked.connect(lambda: self.choose_colour(self.custom_swatch.color))
        self.colour_group.addButton(self.custom_swatch)
        colours.addWidget(self.custom_swatch)
        self.custom_button = make_button(tr("Custom…"), "ghost", "palette", small=True, callback=lambda *_: self._pick_colour())
        colours.addWidget(self.custom_button)
        root.addWidget(colours_host)
        self.colour_note = label(tr("Photos keep their own colours; colour applies to built-in icons and SVGs."), "meta", wrap=True)
        root.addWidget(self.colour_note)

        self.error = label("", wrap=True)
        set_tone(self.error, "danger")
        self.error.hide()
        root.addWidget(self.error)
        buttons = QHBoxLayout()
        buttons.setSpacing(S.sm)
        self.reset_button = make_button(tr("Use default"), "ghost", "refresh", callback=lambda *_: self.reset())
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)
        buttons.addWidget(make_button(tr("Cancel"), "ghost", callback=self.reject))
        self.save_button = make_button(tr("Save"), "primary", callback=self.accept)
        self.save_button.setDefault(True)
        buttons.addWidget(self.save_button)
        root.addLayout(buttons)
        self.resize(520, self.sizeHint().height())
        self._sync()

    # -- choices -----------------------------------------------------------------------------------
    def _is_picture(self) -> bool:
        """A photo (not SVG): it is shown as is, so no colour applies."""
        if self.pending is not None:
            return self.pending[1] != ".svg"
        return bool(self.entry and self.entry["kind"] == "file" and not self.entry["name"].endswith(".svg"))

    def choose_glyph(self, glyph: str) -> None:
        self.pending = None
        self.entry = {"kind": "glyph", "name": glyph, "color": (self.entry or {}).get("color", "")}
        self._sync()

    def choose_colour(self, colour: str) -> None:
        if self._is_picture():
            return
        if self.entry is None:
            self.entry = {"kind": "glyph", "name": self.glyph, "color": ""}
        self.entry["color"] = colour.upper() if colour else ""
        self._sync()

    def _pick_colour(self) -> None:
        colour = QColorDialog.getColor(QColor((self.entry or {}).get("color") or C.accent), self, tr("Custom colour"))
        if colour.isValid():
            self.custom_swatch.color = colour.name().upper()
            self.choose_colour(colour.name())

    def _pick_file(self) -> str:
        """The file dialog (a seam: tests replace it)."""
        path, _ = QFileDialog.getOpenFileName(self, tr("Choose picture or SVG…"), str(Path.home()),
                                              tr("Pictures") + " (*.svg *.png *.jpg *.jpeg *.webp)")
        return path

    def pick(self) -> None:
        path = self._pick_file()
        if not path:
            return
        try:
            self.pending = project_icons.read_source(Path(path))
        except (OSError, ValueError) as error:
            self._say(str(error) if isinstance(error, ValueError) else tr("This picture cannot be read"))
            return
        colour = (self.entry or {}).get("color", "") if self.pending[1] == ".svg" else ""
        self.entry = {"kind": "file", "name": "", "color": colour}      # the stored name comes on Save
        self._sync()

    def reset(self) -> None:
        self.pending, self.entry = None, None
        self._sync()

    def _say(self, text: str) -> None:
        self.error.setText(text)
        self.error.setVisible(bool(text))

    def _sync(self) -> None:
        self._say("")
        glyph = self.entry["name"] if self.entry and self.entry["kind"] == "glyph" else None
        self.glyph_group.setExclusive(False)
        for name, button in self.glyph_buttons.items():
            button.setChecked(name == glyph)
        self.glyph_group.setExclusive(True)
        picture = self._is_picture()
        colour = (self.entry or {}).get("color", "")
        for swatch in (self.default_swatch, self.custom_swatch, *self.swatches.values()):
            swatch.setEnabled(not picture)
        self.custom_button.setEnabled(not picture)
        self.colour_note.setVisible(picture)
        self.default_swatch.color = resolve(self.hue)
        self.colour_group.setExclusive(False)
        for swatch in (self.default_swatch, self.custom_swatch, *self.swatches.values()):
            swatch.setChecked(False)
        self.colour_group.setExclusive(True)
        if not colour:
            self.default_swatch.setChecked(True)
        elif colour in self.swatches:
            self.swatches[colour].setChecked(True)
        else:
            self.custom_swatch.color = colour
            self.custom_swatch.setChecked(True)
        self.preview.update()
        for widget in (self.default_swatch, self.custom_swatch, *self.swatches.values()):
            widget.update()

    # -- result --------------------------------------------------------------------------------------
    def accept(self) -> None:
        if self.name_field is not None:
            name = self.result_name()
            if not name:
                self._say(tr('ui.067e3e6ad298'))
                return
            if name.casefold() in self.taken:
                self._say(tr("A workspace with this name already exists. Choose another name."))
                return
        super().accept()

    def result_name(self) -> str:
        """The edited name (only with ``name_label``), without surrounding spaces or line breaks."""
        return " ".join((self.name_field.text() if self.name_field is not None else "").split())

    def result_entry(self, root: Path) -> dict | None:
        """The entry to keep (None = default folder). Stores a newly picked picture; call once, after Save."""
        if self.entry is None:
            return None
        if self.pending is not None:
            data, extension = self.pending
            return {"kind": "file", "name": project_icons.store_picture(root, data, extension),
                    "color": self.entry.get("color", "") if extension == ".svg" else ""}
        return project_icons.valid_entry(self.entry)
