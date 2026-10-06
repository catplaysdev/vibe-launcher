"""Small dialogs and helpers used when opening a project: pick the account, type a path, create missing folders."""
from __future__ import annotations

from .i18n import tr

import threading
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QStringListModel, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QCompleter, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QVBoxLayout, QWidget)

from . import runtime, theme
from .kit import FilterChip, FlowLayout, ICON_ROLE, TwoLineDelegate, label, make_button, set_tone
from .store import normalize_tags
from .theme import C, S, T

TOOL_LABEL = {"codex": "Codex", "claude": "Claude"}


class AccountChoiceDialog(QDialog):
    """"Which account (and so which service) should this project open with?" — only accounts that can work here."""

    def __init__(self, project_name: str, environment_name: str, options: list[dict[str, Any]], selected: str = "",
                 parent: QWidget | None = None, *, can_skip_next_time: bool = True):
        super().__init__(parent)
        self.setWindowTitle(tr('launch_dialogs.4a791e6a12b3'))
        self.setMinimumWidth(460)
        root = QVBoxLayout(self)
        root.setContentsMargins(S.xl, S.xl, S.xl, S.lg)
        root.setSpacing(S.md)
        heading = QLabel(tr('launch_dialogs.1809a6af6834', v0=f'{project_name}'))
        heading.setProperty("role", "dialogTitle")
        heading.setWordWrap(True)
        root.addWidget(heading)
        root.addWidget(label(tr('launch_dialogs.5c52307888db', v0=f'{environment_name}'), "meta", wrap=True))
        self.listing = QListWidget()
        self.listing.setAccessibleName(tr('launch_dialogs.c5aaa1bbc43e'))
        for option in options:
            tool = TOOL_LABEL.get(option.get("tool", ""), option.get("tool", ""))
            item = QListWidgetItem(f"{tool}　{option['name']}\n{option.get('detail', '')}")
            item.setData(Qt.ItemDataRole.UserRole, option["id"])
            self.listing.addItem(item)
            if option["id"] == selected:
                self.listing.setCurrentItem(item)
        if self.listing.currentItem() is None and self.listing.count():
            self.listing.setCurrentRow(0)
        self.listing.setMinimumHeight(min(260, 26 + 52 * max(1, len(options))))
        self.listing.itemDoubleClicked.connect(lambda *_: self.accept())
        root.addWidget(self.listing)
        self.skip = QCheckBox(tr('launch_dialogs.a2a852f4a55d'))
        self.skip.setVisible(can_skip_next_time)
        root.addWidget(self.skip)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = make_button(tr('launch_dialogs.2cd0f3be8738'), "ghost", callback=lambda *_: self.reject())
        self.open_button = make_button(tr('launch_dialogs.f290f9167ad0'), "primary", callback=lambda *_: self.accept())
        self.open_button.setDefault(True)
        buttons.addWidget(cancel)
        buttons.addWidget(self.open_button)
        root.addLayout(buttons)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        theme.apply_dark_titlebar(self)

    def chosen(self) -> tuple[str, bool]:
        item = self.listing.currentItem()
        return (item.data(Qt.ItemDataRole.UserRole) if item else ""), self.skip.isChecked()


class CreateFoldersDialog(QDialog):
    """Lists the folders on the way to a path, colouring the ones that do not exist yet and would be created."""

    def __init__(self, target: str, existing: Path | None, missing: list[Path], where: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr('launch_dialogs.675f7cca113b'))
        self.setMinimumWidth(520)
        root = QVBoxLayout(self)
        root.setContentsMargins(S.xl, S.xl, S.xl, S.lg)
        root.setSpacing(S.md)
        heading = QLabel(tr('launch_dialogs.d95f670ce0e1'))
        heading.setProperty("role", "dialogTitle")
        root.addWidget(heading)
        root.addWidget(label(f"{where}{target}", "meta", wrap=True))
        self.rows: list[tuple[str, bool]] = []
        box = QVBoxLayout()
        box.setSpacing(S.xs)
        if existing is not None:
            self.rows.append((str(existing), False))
        self.rows.extend((str(folder), True) for folder in missing)
        for text, new in self.rows:
            row = QHBoxLayout()
            tag = QLabel(tr('launch_dialogs.370e6fbb612a') if new else tr('launch_dialogs.e8b1268c1e36'))
            tag.setFixedWidth(56)
            tag.setProperty("role", "badge")
            tag.setProperty("tone", "accent" if new else "")
            tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name = QLabel(text)
            name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            name.setProperty("tone", "accent" if new else "muted")
            row.addWidget(tag)
            row.addWidget(name, 1)
            box.addLayout(row)
        root.addLayout(box)
        if len(missing) > 1:
            root.addWidget(label(tr('launch_dialogs.2887d9cc8a04', v0=f'{len(missing)}'), "meta", wrap=True))
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(make_button(tr('launch_dialogs.ecddc6cb858d'), "ghost", callback=lambda *_: self.reject()))
        create = make_button(tr('launch_dialogs.32b676a6a68e'), "primary", callback=lambda *_: self.accept())
        create.setDefault(True)
        buttons.addWidget(create)
        root.addLayout(buttons)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        theme.apply_dark_titlebar(self)


class TagAssist:
    """Under the tags field: the tags already in use as chips, so a tag is one click instead of retyping it."""

    def __init__(self, dialog, tags_key: str, known: list[str]):
        self.field = dialog.fields[tags_key]
        self.chips: dict[str, FilterChip] = {}
        strip = QWidget()
        flow = FlowLayout(strip, S.xs + 2, S.xs + 2)
        flow.setContentsMargins(0, 0, 0, 0)
        for tag in known:
            chip = FilterChip(tag, "#" + tag, strip)
            chip.clicked.connect(lambda _=False, t=tag: self._toggle(t))
            flow.addWidget(chip)
            self.chips[tag] = chip
        row, _ = dialog.form.getWidgetPosition(self.field)
        strip.setVisible(bool(known))
        dialog.form.insertRow(row + 1, "", strip)
        self.field.textChanged.connect(lambda *_: self.sync())
        self.sync()

    def current(self) -> list[str]:
        try:
            return normalize_tags(self.field.text())
        except ValueError:
            return []

    def sync(self) -> None:
        have = {t.casefold() for t in self.current()}
        for tag, chip in self.chips.items():
            chip.setChecked(tag.casefold() in have)

    def _toggle(self, tag: str) -> None:
        tags = self.current()
        if tag.casefold() in {t.casefold() for t in tags}:
            tags = [t for t in tags if t.casefold() != tag.casefold()]
        else:
            tags.append(tag)
        self.field.setText(", ".join(tags))


class RemoteFolderDialog(QDialog):
    """Pick a folder on an SSH host: the PC cannot browse it, so the listing is fetched over SSH one folder at a time.

    ``lister(path)`` returns ``{"path", "exists", "home", "parent", "dirs", "truncated"}`` or raises (its message is shown).
    It runs on a worker thread, so a slow connection or a password window never freezes this one.
    """

    _loaded = Signal(int, object, str)           # request number, listing (or None), error text

    def __init__(self, host_name: str, start: str, lister: Callable[[str], dict], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr('launch_dialogs.a9f15fcdfc38', v0=f'{host_name}'))
        self.setMinimumSize(520, 520)
        self._lister = lister
        self._ticket = 0
        self._listing: dict | None = None
        self._chosen = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(S.xl, S.xl, S.xl, S.lg)
        root.setSpacing(S.md)
        heading = QLabel(tr('launch_dialogs.73e354de2a89', v0=f'{host_name}'))
        heading.setProperty("role", "dialogTitle")
        heading.setWordWrap(True)
        root.addWidget(heading)
        bar = QHBoxLayout()
        bar.setSpacing(S.sm)
        self.up = make_button(tr('launch_dialogs.f23c7d5713af'), "secondary", "arrow-up-circle", small=True, callback=lambda *_: self._go_up())
        self.home = make_button(tr('launch_dialogs.6a5a0a59efa7'), "secondary", "folder", small=True, callback=lambda *_: self.load("~"))
        self.path = QLineEdit()
        self.path.setPlaceholderText(tr('launch_dialogs.9a352696bbac'))
        self.path.returnPressed.connect(lambda: self.load(self.path.text()))
        self.go = make_button(tr('launch_dialogs.fa96079c3b2f'), "secondary", "arrow-right", small=True, callback=lambda *_: self.load(self.path.text()))
        bar.addWidget(self.up)
        bar.addWidget(self.home)
        bar.addWidget(self.path, 1)
        bar.addWidget(self.go)
        root.addLayout(bar)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.list = QListWidget()
        self.list.setItemDelegate(TwoLineDelegate(self.list))
        self.list.setAccessibleName(tr('launch_dialogs.3a624b5945c3'))
        self.list.itemDoubleClicked.connect(self._enter)
        self.list.itemActivated.connect(self._enter)
        root.addWidget(self.list, 1)
        self.hidden = QCheckBox(tr('launch_dialogs.be6f866ca6fe'))
        self.hidden.toggled.connect(lambda *_: self._fill())
        root.addWidget(self.hidden)
        root.addWidget(label(tr('launch_dialogs.96b88ef6a970'), "meta", wrap=True))
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(make_button(tr('launch_dialogs.2cd0f3be8738'), "ghost", callback=lambda *_: self.reject()))
        self.choose = make_button(tr('launch_dialogs.40c061380602'), "primary", "check", callback=lambda *_: self._accept())
        self.choose.setEnabled(False)
        buttons.addWidget(self.choose)
        root.addLayout(buttons)
        self._loaded.connect(self._on_loaded)
        self.load(start or "~")

    # -- loading ---------------------------------------------------------------------------------
    def load(self, path: str) -> None:
        self._ticket += 1
        ticket = self._ticket
        self.choose.setEnabled(False)
        self.list.setEnabled(False)
        self._say(tr('launch_dialogs.225fdf72b054'), "muted")

        def work() -> None:
            try:
                self._loaded.emit(ticket, self._lister(path.strip() or "~"), "")
            except Exception as error:      # the lister raises messages that are safe to show
                self._loaded.emit(ticket, None, str(error) or type(error).__name__)
        threading.Thread(target=work, daemon=True).start()

    def _on_loaded(self, ticket: int, listing: object, error: str) -> None:
        if ticket != self._ticket:
            return                           # an older answer arriving late
        self.list.setEnabled(True)
        if listing is None:
            self._say(error + tr('launch_dialogs.2ae5dfe1c166'), "danger")
            return
        if not isinstance(listing, dict):
            self._say(tr('launch_dialogs.949eb45de1f0'), "danger")
            return
        self._listing = listing
        self.path.setText(str(listing.get("path", "")))
        self.choose.setEnabled(True)
        self.up.setEnabled(bool(listing.get("parent")))
        if not listing.get("exists", True):
            self._say(tr('launch_dialogs.c3c337e3d50e'), "warn")
        elif listing.get("truncated"):
            self._say(tr('launch_dialogs.75f5f17ac85b'), "warn")
        else:
            self._say("", "muted")
        self._fill()

    def _fill(self) -> None:
        self.list.clear()
        if not self._listing:
            return
        base = str(self._listing.get("path", "")).rstrip("/")
        shown = [n for n in self._listing.get("dirs", []) if self.hidden.isChecked() or not str(n).startswith(".")]
        for name in shown:
            item = QListWidgetItem(str(name))
            item.setData(Qt.ItemDataRole.UserRole, f"{base}/{name}")
            item.setData(ICON_ROLE, ("folder", "accent"))
            self.list.addItem(item)
        if not shown:
            note = QListWidgetItem(tr('launch_dialogs.dc3ee1456f5e'))
            note.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(note)

    def _say(self, text: str, tone: str) -> None:
        self.status.setText(text)
        set_tone(self.status, tone)
        self.status.setVisible(bool(text))

    # -- navigation / result -------------------------------------------------------------------
    def _enter(self, item: QListWidgetItem) -> None:
        target = item.data(Qt.ItemDataRole.UserRole)
        if target:
            self.load(str(target))

    def _go_up(self) -> None:
        if self._listing and self._listing.get("parent"):
            self.load(str(self._listing["parent"]))

    def _accept(self) -> None:
        if self._listing:
            self._chosen = str(self._listing.get("path", ""))
            self.accept()

    def chosen(self) -> str:
        return self._chosen


class PathAssist:
    """Makes typing a project path easier inside the project dialog.

    * suggestions from the paths already used on this environment,
    * a "瀏覽…" button for folders on this PC (disabled for SSH, whose disk this PC cannot browse),
    * a live note: exists / will be asked about / not an absolute path.
    """

    def __init__(self, dialog, path_key: str, environment_key: str, kinds: dict[str, str], known: dict[str, list[str]],
                 browse_remote: Callable[[str, str], str] | None = None):
        self.dialog = dialog
        self.browse_remote = browse_remote          # (environment id, text typed so far) -> chosen path, "" if cancelled
        self.field = dialog.fields[path_key]
        self.environment = dialog.fields[environment_key]
        self.kinds = kinds
        self.known = known
        self.model = QStringListModel(self.dialog)
        completer = QCompleter(self.model, self.dialog)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.field.setCompleter(completer)
        self.browse = make_button(tr('launch_dialogs.e37382f3dec4'), "secondary", "folder", small=True, tip=tr('launch_dialogs.c96c0d4a1f5f'),
                                  callback=lambda *_: self._browse())
        self.note = QLabel()
        self.note.setWordWrap(True)
        strip = QWidget()
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(S.sm)
        layout.addWidget(self.browse)
        layout.addWidget(self.note, 1)
        row, _ = dialog.form.getWidgetPosition(self.field)
        dialog.form.insertRow(row + 1, "", strip)
        self.field.textChanged.connect(lambda *_: self.refresh())
        self.environment.currentIndexChanged.connect(lambda *_: self.refresh())
        self.refresh()

    def environment_id(self) -> str:
        return self.environment.currentData() or "local"

    def is_local(self) -> bool:
        return self.kinds.get(self.environment_id(), "local") == "local"

    def _browse(self) -> None:
        if not self.is_local():
            if self.browse_remote is not None:
                chosen = self.browse_remote(self.environment_id(), self.field.text().strip())
                if chosen:
                    self.field.setText(chosen)
            return
        start = self.field.text().strip()
        if not start or not Path(start).exists():
            start = str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self.dialog, tr('launch_dialogs.008db57ff7cb'), start)
        if chosen:
            self.field.setText(str(Path(chosen)))

    def _say(self, text: str, tone: str) -> None:
        self.note.setText(text)
        set_tone(self.note, tone)

    def refresh(self) -> None:
        local = self.is_local()
        self.browse.setEnabled(local or self.browse_remote is not None)
        self.browse.setToolTip(tr('launch_dialogs.c96c0d4a1f5f') if local else tr('launch_dialogs.ccce95a01374'))
        self.model.setStringList(self.known.get(self.environment_id(), []))
        text = self.field.text().strip()
        if not text:
            self._say(tr('launch_dialogs.f4f5b211c291') + ("。" if local else tr('launch_dialogs.ccde1bc12dd9')), "muted")
        elif not local:
            ok = text.startswith(("/", "~"))
            self._say(tr('launch_dialogs.66b780feefc0') if ok
                      else tr('launch_dialogs.703ecc754aaa'),
                      "muted" if ok else "warn")
        elif not Path(text).expanduser().is_absolute():
            self._say(tr('launch_dialogs.4aa4d7c8ed85'), "warn")
        elif Path(text).expanduser().is_dir():
            self._say(tr('launch_dialogs.b64589adcf34'), "ok")
        elif Path(text).expanduser().exists():
            self._say(tr('launch_dialogs.a779256042ab'), "danger")
        else:
            try:
                _, missing = runtime.folder_plan(Path(text).expanduser())
                self._say(tr('launch_dialogs.8ce50619aca8', v0=f'{len(missing)}'), "warn")
            except ValueError as error:
                self._say(str(error), "danger")
