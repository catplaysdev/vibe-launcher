"""Central settings: one window with an icon list of categories; changes are saved only after validation.

Appearance edits preview live on the whole window; Cancel puts back what was there when the dialog opened.
"""
from __future__ import annotations

import json

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QKeySequenceEdit, QLabel, QListWidget,
    QListWidgetItem, QPlainTextEdit, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from . import icons, settings, shortcuts
from .appearance import AppearancePanel
from .i18n import LANGUAGES, tr
from .kit import SearchField, label, make_button, set_tone
from .theme import UI_SCALES, C, S, theme_bus

# Shortcut actions grouped the way people look for them.
SHORTCUT_GROUPS = (
    ("Application", ("palette", "search", "create", "sidebar", "settings", "window.next", "window.prev",
                     *[f"page:{i}" for i in range(1, 6)])),
    ("Workspace terminals", ("next", "prev", "workspace.palette", "new", "detach", "close", "composer",
                             "composer.position", "zoom.in", "zoom.out", "zoom.reset",
                             *[f"goto:{i}" for i in range(1, 10)])),
    ("Focus navigation", ("navigate", "nav.up", "nav.down", "nav.left", "nav.right", "nav.input")),
)


def _page(title: str, subtitle: str) -> tuple[QScrollArea, QVBoxLayout]:
    """A scrollable settings page with its heading."""
    body = QWidget()
    layout = QVBoxLayout(body)
    layout.setContentsMargins(0, 0, S.md, 0)
    layout.setSpacing(S.md)
    layout.addWidget(label(title, "dialogTitle", wrap=True))
    if subtitle:
        layout.addWidget(label(subtitle, "muted", wrap=True))
    layout.addSpacing(S.xs)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    # The application normally hides horizontal bars. Long translated controls
    # still need an accessible fallback when this dialog is narrowed.
    scroll.setStyleSheet("""
        QScrollBar:horizontal { height: 10px; background: transparent; }
        QScrollBar::handle:horizontal { background: #8190A8; border-radius: 3px; min-width: 32px; }
    """)
    scroll.setWidget(body)
    return scroll, layout


def _group(layout: QVBoxLayout, title: str) -> QFormLayout:
    layout.addSpacing(S.sm)
    layout.addWidget(label(title, "subsection", wrap=True))
    form = QFormLayout()
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    form.setSpacing(S.md)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    layout.addLayout(form)
    return form


class SettingsDialog(QDialog):
    PAGES = (("general", "General", "sliders"), ("appearance", "Appearance", "palette"),
             ("terminal", "Terminal", "terminal"), ("keyboard", "Keyboard shortcuts", "keyboard"),
             ("extensions", "Extensions", "puzzle"), ("about", "About and updates", "info"))

    def __init__(self, window, page: str = "general"):
        super().__init__(window)
        self.window = window
        self.setWindowTitle(tr("Settings"))
        self.resize(880, 640)
        self.setMinimumSize(640, 480)
        self.data = settings.load_settings(window.store.path.parent)
        self.original_appearance = dict(window.appearance_settings())

        root = QHBoxLayout(self)
        root.setContentsMargins(S.lg, S.lg, S.lg, S.lg)
        root.setSpacing(S.lg)
        self.nav = QListWidget()
        self.nav.setAccessibleName(tr("Settings categories"))
        self.nav.setFixedWidth(200)
        self.nav.setIconSize(QSize(18, 18))
        root.addWidget(self.nav)
        right = QVBoxLayout()
        right.setSpacing(S.md)
        self.stack = QStackedWidget()
        right.addWidget(self.stack, 1)
        self.error = label("", wrap=True)
        set_tone(self.error, "danger")
        self.error.hide()
        right.addWidget(self.error)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(make_button(tr("Cancel"), "ghost", callback=self.reject))
        self.save_button = make_button(tr("Save"), "primary", callback=self.save)
        self.save_button.setDefault(True)
        buttons.addWidget(self.save_button)
        right.addLayout(buttons)
        root.addLayout(right, 1)

        self._build_general()
        self._build_appearance()
        self._build_terminal()
        self._build_keyboard()
        self._build_extensions()
        self._build_about()
        for key, title, glyph in self.PAGES:
            item = QListWidgetItem(tr(title))
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setData(Qt.ItemDataRole.UserRole + 1, glyph)
            self.nav.addItem(item)
        self._paint_nav_icons()
        theme_bus().changed.connect(self._paint_nav_icons)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.show_page(page)

    def _paint_nav_icons(self) -> None:
        for row in range(self.nav.count()):
            item = self.nav.item(row)
            item.setIcon(icons.icon(item.data(Qt.ItemDataRole.UserRole + 1), C.text_secondary, 18))

    def show_page(self, key: str) -> None:
        keys = [key for key, _, _ in self.PAGES]
        self.nav.setCurrentRow(keys.index(key) if key in keys else 0)

    # -- pages ------------------------------------------------------------------------------------
    def _build_general(self) -> None:
        page, layout = _page(tr("General"), tr("Language and how terminals open."))
        form = _group(layout, tr("Language"))
        self.language = QComboBox()
        for code, name in LANGUAGES.items():
            self.language.addItem(name, code)
        self.language.setCurrentIndex(self.language.findData(self.window.prefs.language))
        self.language.setAccessibleName(tr("Language"))
        form.addRow(tr("Interface language"), self.language)
        form.addRow(label(tr("Language changes take effect after restarting the launcher."), "meta", wrap=True))
        form = _group(layout, tr("Display"))
        self.ui_scale = QComboBox()
        for percent in UI_SCALES:
            self.ui_scale.addItem(f"{percent}%", percent)
        self.ui_scale.setCurrentIndex(max(0, self.ui_scale.findData(getattr(self.window.prefs, "ui_scale", 100))))
        self.ui_scale.setAccessibleName(tr("Interface scale"))
        self.ui_scale.currentIndexChanged.connect(self._preview_scale)
        form.addRow(tr("Interface scale"), self.ui_scale)
        # The window's top-left corner as it will look at the chosen scale (a picture: the window itself only changes
        # after a restart, because Qt takes the scale when the application starts).
        self.scale_preview = QLabel()
        self.scale_preview.setObjectName("scalePreview")
        self.scale_preview.setFixedHeight(self.PREVIEW.height())
        self.scale_preview.setMaximumWidth(self.PREVIEW.width())
        self.scale_preview.setMinimumWidth(0)
        self.scale_preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.scale_preview.setFrameShape(QFrame.Shape.StyledPanel)
        self.scale_preview.setAccessibleName(tr("Interface scale preview"))
        form.addRow(self.scale_preview)
        keys = shortcuts.bindings()
        form.addRow(label(tr("Enlarges or shrinks the whole window, terminals included. Applied when the launcher restarts; "
                             "after saving you can restart right away. To zoom only terminal text, press {v0} / {v1} "
                             "or hold Ctrl and scroll in a terminal.", v0=keys["zoom.in"], v1=keys["zoom.out"]), "meta", wrap=True))
        self._preview_scale()
        form = _group(layout, tr("Terminals"))
        self.external = QCheckBox(tr("Open terminals in separate windows"))
        self.external.setChecked(self.window.prefs.external_terminal)
        form.addRow(self.external)
        form = _group(layout, tr("Updates"))
        self.update_check = QCheckBox(tr("Check for new versions automatically"))
        self.update_check.setChecked(bool(getattr(self.window.prefs, "update_check", True)))
        form.addRow(self.update_check)
        layout.addSpacing(S.sm)
        layout.addWidget(label(tr("Accounts and environments have their own pages."), "subsection", wrap=True))
        row = QFormLayout()
        row.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        links = []
        for text, target, glyph in (("Accounts and integrations", "accounts", "user"),
                                    ("Environments and SSH", "environments", "layers")):
            links.append(make_button(tr(text), "secondary", glyph, callback=lambda *_, t=target: self.go_to(t)))
        row.addRow(*links)
        layout.addLayout(row)
        layout.addStretch(1)
        self.stack.addWidget(page)

    PREVIEW = QSize(440, 180)

    def _preview_scale(self, *_) -> None:
        from .ui import applied_ui_scale
        ratio = (self.ui_scale.currentData() or 100) / applied_ui_scale()
        box = self.PREVIEW
        source = QRect(0, 0, round(box.width() / ratio), round(box.height() / ratio)) & self.window.rect()
        picture = self.window.grab(source)
        dpr = picture.devicePixelRatio()
        picture = picture.scaled(QSize(round(source.width() * ratio * dpr), round(source.height() * ratio * dpr)),
                                 Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        picture.setDevicePixelRatio(dpr)
        self.scale_preview.setPixmap(picture)

    def _build_appearance(self) -> None:
        page, layout = _page(tr("Appearance"), tr("Changes preview on the whole window right away."))
        self.appearance = AppearancePanel(self.original_appearance)
        self.appearance.changed.connect(lambda values: self.window.apply_appearance(values, save=False))
        layout.addWidget(self.appearance)
        layout.addStretch(1)
        self.stack.addWidget(page)

    def _build_terminal(self) -> None:
        page, layout = _page(tr("Terminal"), tr("Use the Terminal menu to choose a shell or CLI for one launch. Commands run on the project's environment."))
        form = _group(layout, tr("Default shells"))
        self.shells = {}
        for platform, name in (("windows", "Windows shell"), ("linux", "Linux / SSH shell")):
            combo = QComboBox()
            combo.setAccessibleName(tr(name))
            self.shells[platform] = combo
            for profile in settings.BUILTIN_PROFILES + self.data["plugins"]:
                if platform in profile["platforms"]:
                    combo.addItem(tr(profile["label"]), profile["id"])
            combo.setCurrentIndex(combo.findData(self.data["shells"][platform]))
            form.addRow(tr(name), combo)
        form = _group(layout, tr("Input bar"))
        self.position = QComboBox()
        self.position.addItem(tr("Above terminal"), "top")
        self.position.addItem(tr("Below terminal"), "bottom")
        self.position.setCurrentIndex(self.position.findData(self.window.prefs.composer_position))
        self.position.setAccessibleName(tr("Input bar position"))
        form.addRow(tr("Input bar position"), self.position)
        layout.addStretch(1)
        self.stack.addWidget(page)

    def _build_keyboard(self) -> None:
        page, layout = _page(tr("Keyboard shortcuts"), tr("Click a field and press the new key combination."))
        self.key_search = SearchField(tr("Search shortcuts"))
        self.key_search.textChanged.connect(self._filter_keys)
        layout.addWidget(self.key_search)
        bindings = {**shortcuts.DEFAULTS, **self.window.prefs.shortcuts}
        self.key_edits: dict[str, QKeySequenceEdit] = {}
        self.key_rows: dict[str, tuple[QFormLayout, int, QLabel]] = {}
        self.key_groups: list[tuple[QLabel, QFormLayout]] = []
        for title, actions in SHORTCUT_GROUPS:
            heading = label(tr(title), "subsection", wrap=True)
            layout.addSpacing(S.xs)
            layout.addWidget(heading)
            form = QFormLayout()
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setSpacing(S.sm)
            layout.addLayout(form)
            self.key_groups.append((heading, form))
            for action in actions:
                edit = QKeySequenceEdit(QKeySequence(bindings[action]))
                edit.setMaximumSequenceLength(1)
                edit.setAccessibleName(tr(shortcuts.LABELS[action]))
                edit.keySequenceChanged.connect(self._check_keys)
                name = label(tr(shortcuts.LABELS[action]), wrap=True)
                form.addRow(name, edit)
                self.key_edits[action] = edit
                self.key_rows[action] = (form, form.rowCount() - 1, name)
        layout.addSpacing(S.sm)
        layout.addWidget(make_button(tr("Restore default shortcuts"), "secondary", "refresh", callback=self.reset_shortcuts),
                         0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        self.stack.addWidget(page)

    def _build_extensions(self) -> None:
        page, layout = _page(tr("Extensions"), tr("CLI plugins are command profiles. Configure the tool's dedicated profile environment variable; sign in through its official CLI. No credentials belong in this file."))
        self.plugins = QPlainTextEdit(json.dumps(self.data["plugins"], ensure_ascii=False, indent=2))
        self.plugins.setAccessibleName(tr("CLI plugin manifests (JSON)"))
        self.plugins.setMinimumHeight(260)
        layout.addWidget(self.plugins, 1)
        row = QHBoxLayout()
        row.addWidget(make_button(tr("Insert CLI plugin example"), "secondary", "plus", callback=self.insert_example))
        row.addStretch(1)
        layout.addLayout(row)
        path = label(str(self.window.store.path.parent / "settings.json"), "meta", wrap=True)
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(path)
        self.stack.addWidget(page)

    def _build_about(self) -> None:
        from .about import AboutPanel
        page, layout = _page(tr("About and updates"), "")
        self.about = AboutPanel(self.window)
        layout.addWidget(self.about)
        layout.addStretch(1)
        self.stack.addWidget(page)

    # -- shortcuts --------------------------------------------------------------------------------
    def _filter_keys(self, text: str) -> None:
        query = text.strip().casefold()
        for action, (form, row, name) in self.key_rows.items():
            sequence = self.key_edits[action].keySequence().toString(QKeySequence.SequenceFormat.NativeText)
            form.setRowVisible(row, not query or query in name.text().casefold() or query in sequence.casefold())
        for heading, form in self.key_groups:
            heading.setVisible(any(form.isRowVisible(r) for r in range(form.rowCount())))

    def _key_bindings(self) -> dict[str, str]:
        return {action: edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
                for action, edit in self.key_edits.items()}

    def _check_keys(self, *_) -> bool:
        """Mark conflicting or invalid rows as they are edited; Save stays off until they are fixed."""
        for _, _, name in self.key_rows.values():
            set_tone(name, "")
        try:
            shortcuts.validate(self._key_bindings())
        except ValueError as error:
            message = str(error)
            for action in message.split(": ", 1)[-1].split(" / "):
                if action in self.key_rows:
                    set_tone(self.key_rows[action][2], "danger")
            self._say(message)
            self.save_button.setEnabled(False)
            return False
        self._say("")
        self.save_button.setEnabled(True)
        return True

    def reset_shortcuts(self) -> None:
        for action, edit in self.key_edits.items():
            edit.setKeySequence(QKeySequence(shortcuts.DEFAULTS[action]))
        self._check_keys()

    # -- extensions -------------------------------------------------------------------------------
    def insert_example(self) -> None:
        try:
            plugins = json.loads(self.plugins.toPlainText())
            if not isinstance(plugins, list):
                raise ValueError("Plugin manifests must be a JSON array")
            ids = {p.get("id") for p in plugins if isinstance(p, dict)}
            if "example-cli" not in ids:
                plugins.append({"id": "example-cli", "label": "Example Agent CLI", "platforms": ["windows", "linux"],
                                "command": ["example-agent"], "profile_env": "EXAMPLE_AGENT_HOME"})
            self.plugins.setPlainText(json.dumps(plugins, ensure_ascii=False, indent=2))
        except (ValueError, TypeError) as error:
            self._say(str(error))

    # -- closing ----------------------------------------------------------------------------------
    def _say(self, text: str) -> None:
        self.error.setText(text)
        self.error.setVisible(bool(text))

    def go_to(self, page: str) -> None:
        self.reject()
        self.window.show_page(page)

    def reject(self) -> None:
        if self.appearance.settings != self.original_appearance:
            self.window.apply_appearance(self.original_appearance, save=False)
        super().reject()

    def save(self) -> None:
        try:
            bindings = shortcuts.validate(self._key_bindings())
            data = settings.validate_settings({**self.data, "shells": {p: combo.currentData() for p, combo in self.shells.items()},
                                               "plugins": json.loads(self.plugins.toPlainText())})
            settings.save_settings(self.window.store.path.parent, data)
            prefs = self.window.prefs
            prefs.language = self.language.currentData()
            prefs.shortcuts = {k: v for k, v in bindings.items() if v != shortcuts.DEFAULTS[k]}
            prefs.composer_position = self.position.currentData()
            prefs.external_terminal = self.external.isChecked()
            scale_changed = self.ui_scale.currentData() != getattr(prefs, "ui_scale", 100)
            prefs.ui_scale = self.ui_scale.currentData()
            if hasattr(prefs, "update_check"):
                prefs.update_check = self.update_check.isChecked()
            prefs.save()
            self.window.apply_appearance(self.appearance.settings, save=True)
            self.window.apply_controls()
            self.accept()
            if scale_changed and self.window._confirm(
                    tr("Interface scale"), tr("The new interface scale is applied when the launcher restarts. Restart now?"),
                    confirm_text=tr("Restart now")):
                self.window.restart_launcher()
        except (OSError, ValueError, TypeError) as error:
            self._say(str(error))
