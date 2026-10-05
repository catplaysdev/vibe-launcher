"""Small dialogs and helpers used when opening a project: pick the account, type a path, create missing folders."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QStringListModel, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QCompleter, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QVBoxLayout, QWidget)

from . import runtime, theme
from .kit import FilterChip, FlowLayout, ICON_ROLE, TwoLineDelegate, label, make_button
from .store import normalize_tags
from .theme import C, S, T

TOOL_LABEL = {"codex": "Codex", "claude": "Claude"}


def _dark(dialog: QDialog) -> None:
    dialog.setStyleSheet(f"QLabel {{ color: {C.text_secondary}; }}")


class AccountChoiceDialog(QDialog):
    """"Which account (and so which service) should this project open with?" — only accounts that can work here."""

    def __init__(self, project_name: str, environment_name: str, options: list[dict[str, Any]], selected: str = "",
                 parent: QWidget | None = None, *, can_skip_next_time: bool = True):
        super().__init__(parent)
        self.setWindowTitle("選擇帳號")
        self.setMinimumWidth(460)
        _dark(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(S.xl, S.xl, S.xl, S.lg)
        root.setSpacing(S.md)
        heading = QLabel(f"用哪個帳號開啟「{project_name}」？")
        heading.setProperty("role", "section")
        heading.setStyleSheet(f"font-size: {T.dialog_title}px; color: {C.text};")
        heading.setWordWrap(True)
        root.addWidget(heading)
        root.addWidget(label(f"環境：{environment_name}。訂閱帳號的登入只存在它綁定的環境，所以這裡只列出能在此使用的帳號。", "meta", wrap=True))
        self.listing = QListWidget()
        self.listing.setAccessibleName("可用帳號")
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
        self.skip = QCheckBox("下次直接使用這個帳號，不再詢問")
        self.skip.setVisible(can_skip_next_time)
        root.addWidget(self.skip)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = make_button("取消", "ghost", callback=lambda *_: self.reject())
        self.open_button = make_button("開啟", "primary", callback=lambda *_: self.accept())
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
        self.setWindowTitle("資料夾不存在")
        self.setMinimumWidth(520)
        _dark(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(S.xl, S.xl, S.xl, S.lg)
        root.setSpacing(S.md)
        heading = QLabel("資料夾還不存在，要建立嗎？")
        heading.setProperty("role", "section")
        heading.setStyleSheet(f"font-size: {T.dialog_title}px; color: {C.text};")
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
            tag = QLabel("將建立" if new else "已存在")
            tag.setFixedWidth(56)
            tag.setStyleSheet(f"color: {C.accent_ink if new else C.text_muted}; font-size: {T.micro}px;"
                              + (f" background: {C.accent}; border-radius: 6px; padding: 1px 6px;" if new else ""))
            tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name = QLabel(text)
            name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            name.setStyleSheet(f"color: {C.accent if new else C.text_muted}; font-weight: {600 if new else 400};")
            row.addWidget(tag)
            row.addWidget(name, 1)
            box.addLayout(row)
        root.addLayout(box)
        if len(missing) > 1:
            root.addWidget(label(f"共 {len(missing)} 層資料夾會被建立（標示為「將建立」的部分）。", "meta", wrap=True))
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(make_button("先不要", "ghost", callback=lambda *_: self.reject()))
        create = make_button("建立資料夾", "primary", callback=lambda *_: self.accept())
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
        self.setWindowTitle(f"選擇資料夾 · {host_name}")
        self.setMinimumSize(520, 520)
        _dark(self)
        self._lister = lister
        self._ticket = 0
        self._listing: dict | None = None
        self._chosen = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(S.xl, S.xl, S.xl, S.lg)
        root.setSpacing(S.md)
        heading = QLabel(f"在「{host_name}」選擇專案資料夾")
        heading.setProperty("role", "section")
        heading.setStyleSheet(f"font-size: {T.dialog_title}px; color: {C.text};")
        heading.setWordWrap(True)
        root.addWidget(heading)
        bar = QHBoxLayout()
        bar.setSpacing(S.sm)
        self.up = make_button("上一層", "secondary", "arrow-up-circle", small=True, callback=lambda *_: self._go_up())
        self.home = make_button("家目錄", "secondary", "folder", small=True, callback=lambda *_: self.load("~"))
        self.path = QLineEdit()
        self.path.setPlaceholderText("/home/me/project 或 ~/project")
        self.path.returnPressed.connect(lambda: self.load(self.path.text()))
        self.go = make_button("前往", "secondary", "arrow-right", small=True, callback=lambda *_: self.load(self.path.text()))
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
        self.list.setAccessibleName("主機上的資料夾")
        self.list.itemDoubleClicked.connect(self._enter)
        self.list.itemActivated.connect(self._enter)
        root.addWidget(self.list, 1)
        self.hidden = QCheckBox("顯示隱藏資料夾（. 開頭）")
        self.hidden.toggled.connect(lambda *_: self._fill())
        root.addWidget(self.hidden)
        root.addWidget(label("雙擊資料夾進入；也可以直接輸入路徑後按「前往」。選定後按「選擇目前這個資料夾」。", "meta", wrap=True))
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(make_button("取消", "ghost", callback=lambda *_: self.reject()))
        self.choose = make_button("選擇目前這個資料夾", "primary", "check", callback=lambda *_: self._accept())
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
        self._say("連線到主機並讀取資料夾…（需要驗證時會跳出視窗）", C.text_muted)

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
            self._say(error + "　（可以按「前往」重試，或直接在專案設定輸入路徑）", C.danger)
            return
        if not isinstance(listing, dict):
            self._say("主機回傳的資料無法解讀。", C.danger)
            return
        self._listing = listing
        self.path.setText(str(listing.get("path", "")))
        self.choose.setEnabled(True)
        self.up.setEnabled(bool(listing.get("parent")))
        if not listing.get("exists", True):
            self._say("這個路徑在主機上不存在，已顯示最近的上層資料夾。選擇它也可以；之後輸入完整路徑時，啟動會詢問是否建立。", C.warning)
        elif listing.get("truncated"):
            self._say("資料夾很多，只列出前面一部分。可以直接輸入路徑。", C.warning)
        else:
            self._say("", C.text_muted)
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
            note = QListWidgetItem("（沒有子資料夾）")
            note.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(note)

    def _say(self, text: str, color: str) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")
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
        self.browse = make_button("瀏覽…", "secondary", "folder", small=True, tip="選擇這台電腦上的資料夾",
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
        chosen = QFileDialog.getExistingDirectory(self.dialog, "選擇專案資料夾", start)
        if chosen:
            self.field.setText(str(Path(chosen)))

    def _say(self, text: str, color: str) -> None:
        self.note.setText(text)
        self.note.setStyleSheet(f"color: {color};")

    def refresh(self) -> None:
        local = self.is_local()
        self.browse.setEnabled(local or self.browse_remote is not None)
        self.browse.setToolTip("選擇這台電腦上的資料夾" if local else "連線到這台主機，瀏覽並選擇資料夾（需要驗證時會跳出視窗）")
        self.model.setStringList(self.known.get(self.environment_id(), []))
        text = self.field.text().strip()
        if not text:
            self._say("輸入完整路徑，或按「瀏覽…」選擇" + ("。" if local else "（會連線到主機）；也可以直接輸入，例如 /home/me/project 或 ~/project。"), C.text_muted)
        elif not local:
            ok = text.startswith(("/", "~"))
            self._say("SSH 主機上的路徑會在啟動時確認；不存在時會詢問是否建立。可按「瀏覽…」看它在主機上的樣子。" if ok
                      else "請輸入主機上的完整路徑（以 / 或 ~ 開頭），或按「瀏覽…」選擇。",
                      C.text_muted if ok else C.warning)
        elif not Path(text).expanduser().is_absolute():
            self._say("請輸入完整路徑，例如 C:\\Projects\\my-app。", C.warning)
        elif Path(text).expanduser().is_dir():
            self._say("✓ 資料夾已存在。", C.success)
        elif Path(text).expanduser().exists():
            self._say("這個路徑是檔案，不是資料夾。", C.danger)
        else:
            try:
                _, missing = runtime.folder_plan(Path(text).expanduser())
                self._say(f"這個資料夾不存在（會建立 {len(missing)} 層）。儲存時會詢問是否建立。", C.warning)
            except ValueError as error:
                self._say(str(error), C.danger)
