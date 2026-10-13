"""Grouped account identities; storage and account operations belong to the window."""

from __future__ import annotations

from .i18n import tr

import json
from typing import Any

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMenu, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from .kit import IconTile, PageHeader, SearchField, dispose_widget, make_button
from .theme import R, S, TOOL_ICONS, section_hue, tool_hue

TOOLS = (("codex", "Codex"), ("claude", "Claude"),
         ("deepseek-harness", tr('accounts_page.17133d073aed')), ("github", "GitHub"))
VISIBLE_ROWS = 4        # an environment plate shows this many accounts, then scrolls
STATUS = {"authenticated": tr('accounts_page.c42a66e27201'), "configured": tr('accounts_page.40857ec8a467'), "failed": tr('accounts_page.82f6be16f3b4'),
          "unavailable": tr('accounts_page.ee95db46cf38'), "unknown": tr('accounts_page.ee95db46cf38'), "logged_out": tr('accounts_page.4e852fca0381')}


class AccountsPage(QWidget):
    add_requested = Signal(str)
    selected = Signal(str, str)
    action_requested = Signal(str, str, str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._records: list[dict[str, Any]] = []
        self._environments: dict[str, str] = {}
        self._states: dict[str, str] = {}
        self._signature = ""
        self._selection = ("", "")
        self._columns = 0
        self.plates: dict[str, QFrame] = {}
        self.lists: dict[str, QListWidget] = {}         # "<tool>/<environment id>" -> its accounts
        self.context_requested = None                  # set by the window: (kind, record_id, global position)
        root = QVBoxLayout(self)
        root.setContentsMargins(S.lg, S.lg, S.lg, S.lg)
        root.setSpacing(S.md)
        header = PageHeader(tr('accounts_page.969396f78f3b'), tr('accounts_page.f085b02d7611'), icon="user", hue=section_hue("accounts"))
        self.add_button = make_button(tr('accounts_page.fac920aaecbc'), "primary", "plus")
        self.add_menu = QMenu(self.add_button)
        for tool, title in TOOLS:
            self.add_menu.addAction(title, lambda tool=tool: self.add_requested.emit(tool))
        self.add_button.setMenu(self.add_menu)
        header.add_action(self.add_button)
        root.addWidget(header)
        filters = QHBoxLayout()
        self.search = SearchField(tr('accounts_page.345d870dc2ee'))
        self.environment = QComboBox()
        self.environment.setAccessibleName(tr('accounts_page.52db1a1da5fd'))
        self.environment.addItem(tr('accounts_page.a211197f2b30'), "")
        filters.addWidget(self.search, 1)
        filters.addWidget(self.environment)
        root.addLayout(filters)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().installEventFilter(self)
        self.content = QWidget()
        content = QVBoxLayout(self.content)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(S.md)
        self.plate_host = QWidget()
        self.grid = QGridLayout(self.plate_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(S.md)
        content.addWidget(self.plate_host)
        self.empty = QLabel(tr('accounts_page.23fa9718b35c'))
        self.empty.setWordWrap(True)
        self.empty.setProperty("role", "muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(self.empty)
        self.details_host = QFrame()
        self.details_host.setProperty("surface", "card")
        self.details_layout = QVBoxLayout(self.details_host)
        self.details_layout.setContentsMargins(S.md, S.md, S.md, S.md)
        self.details_host.hide()
        content.addWidget(self.details_host)
        content.addStretch()
        self.scroll.setWidget(self.content)
        root.addWidget(self.scroll, 1)
        self.search.textChanged.connect(self._render)
        self.environment.currentIndexChanged.connect(self._render)

    @property
    def current_selection(self) -> tuple[str, str]:
        return self._selection

    def add_detail_widget(self, widget: QWidget) -> None:
        self.details_layout.addWidget(widget)

    def request_action(self, action: str) -> None:
        kind, record_id = self._selection
        if record_id:
            self.action_requested.emit(action, kind, record_id)

    def set_records(self, accounts: list[dict], github_accounts: list[dict],
                    environments: list[dict], auth_states: list[dict]) -> None:
        signature = json.dumps([accounts, github_accounts, environments, auth_states], sort_keys=True, ensure_ascii=False)
        if signature == self._signature:
            return
        self._signature = signature
        self._records = [{**a, "_kind": "accounts"} for a in accounts]
        self._records += [{**a, "tool": "github", "_kind": "github_accounts"} for a in github_accounts]
        self._environments = {e["id"]: e.get("name", e["id"]) for e in environments}
        self._states = {s["id"]: s.get("status", "unknown") for s in auth_states}
        selected_environment = self.environment.currentData()
        self.environment.blockSignals(True)
        self.environment.clear()
        self.environment.addItem(tr('accounts_page.a211197f2b30'), "")
        for env_id, name in sorted(self._environments.items(), key=lambda pair: pair[1].casefold()):
            self.environment.addItem(name, env_id)
        self.environment.setCurrentIndex(max(0, self.environment.findData(selected_environment)))
        self.environment.blockSignals(False)
        self._render()

    def select(self, kind: str, record_id: str) -> None:
        previous = self._selection
        self._selection = (kind, record_id)
        self._sync_selection()
        if self._selection != previous:
            self.selected.emit(*self._selection)

    def _environment_name(self, env_id: str) -> str:
        return self._environments.get(env_id, env_id or tr('accounts_page.2d1afa1675e1'))

    def _row_text(self, account: dict, with_environment: bool = True) -> str:
        env_id = account.get("environment_id", "")
        env = self._environment_name(env_id)
        if account.get("tool") == "github":
            status = STATUS.get(account.get("status", "unknown"), tr('accounts_page.ee95db46cf38'))
            identity = account.get("login") or tr('accounts_page.9c24e74a1b6f')
        elif account.get("tool") == "deepseek-harness":
            status, identity = tr('accounts_page.3c82da098f7f'), ""
        elif account.get("auth") == "api":
            status, identity = tr('accounts_page.40857ec8a467'), account.get("model", "")
        else:
            state = self._states.get(f"{account['id']}:{env_id}")
            if not env_id:
                state = next((value for key, value in self._states.items() if key.startswith(account['id'] + ':')), None)
            status = STATUS.get(state or "unknown", tr('accounts_page.ee95db46cf38'))
            identity = ""
        return "\n".join(filter(None, [account.get("name") or tr('accounts_page.67bee6acfc3c'),
                                       f"{status} · {env}" if with_environment else status, identity]))

    def _render(self, *_args) -> None:
        query = self.search.text().strip().casefold()
        environment = self.environment.currentData()
        records = [a for a in self._records if (not environment or not a.get("environment_id")
                   or a.get("environment_id") == environment)
                   and (not query or query in self._row_text(a).casefold())]
        for plate in self.plates.values():
            self.grid.removeWidget(plate)
            dispose_widget(plate)
        self.plates = {}
        self.lists = {}
        for tool, title in TOOLS:
            group = sorted([a for a in records if a.get("tool") == tool], key=lambda a: a.get("name", "").casefold())
            if not group:
                continue
            plate = QFrame()
            plate.setProperty("surface", "card")
            plate.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            layout = QVBoxLayout(plate)
            layout.setContentsMargins(S.md, S.md, S.md, S.md)
            head = QHBoxLayout()
            head.setSpacing(S.sm + 2)
            tile = IconTile(30, 16, radius=R.md)
            tile.set_icon(TOOL_ICONS.get(tool, "user"), tool_hue(tool))
            head.addWidget(tile)
            heading = QLabel(f"{title}  ·  {len(group)}")
            heading.setProperty("role", "section")
            head.addWidget(heading, 1)
            layout.addLayout(head)
            by_environment: dict[str, list[dict]] = {}
            for account in group:
                by_environment.setdefault(account.get("environment_id") or "", []).append(account)
            # This PC first, then servers by name; accounts usable anywhere (API keys) last.
            order = sorted(by_environment, key=lambda env_id: (env_id == "", env_id != "local", self._environment_name(env_id).casefold()))
            for env_id in order:
                layout.addWidget(self._environment_plate(tool, title, env_id, by_environment[env_id]))
            self.plates[tool] = plate
        self.empty.setVisible(not self.plates)
        self.empty.setText(tr('accounts_page.6ea3333e5ab2') if self._records else tr('accounts_page.23fa9718b35c'))
        self._columns = 0
        self._relayout()
        valid = {(a["_kind"], a["id"]) for a in records}
        if self._selection not in valid:
            had_selection = bool(self._selection[1])
            self._selection = ("", "")
            if had_selection:
                self.selected.emit("", "")
        self._sync_selection()

    def _environment_plate(self, tool: str, title: str, env_id: str, accounts: list[dict]) -> QFrame:
        plate = QFrame()
        plate.setProperty("surface", "well")
        layout = QVBoxLayout(plate)
        layout.setContentsMargins(S.sm, S.sm, S.sm, S.sm)
        layout.setSpacing(S.xs)
        name = self._environment_name(env_id)
        heading = QLabel(f"{name}  ·  {len(accounts)}")
        heading.setProperty("role", "subsection")
        layout.addWidget(heading)
        listing = QListWidget()
        listing.setAccessibleName(tr('accounts_page.d3f30c3ed64f', v0=f'{title} · {name}'))
        listing.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        listing.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        listing.setWordWrap(True)
        listing.setMinimumWidth(0)
        listing.installEventFilter(self)
        listing.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        listing.customContextMenuRequested.connect(lambda pos, listing=listing: self._context_menu(listing, pos))
        heights = []
        for account in accounts:
            item = QListWidgetItem(self._row_text(account, with_environment=False))
            item.setData(Qt.ItemDataRole.UserRole, (account["_kind"], account["id"]))
            item.setToolTip(self._row_text(account))
            height = 82 if item.text().count("\n") >= 2 else 62
            item.setSizeHint(QSize(0, height))
            listing.addItem(item)
            heights.append(height)
        # The theme pads the list by S.xs and spaces rows by a 2px margin above and below each.
        listing.setFixedHeight(2 * listing.frameWidth() + 2 * S.xs + sum(height + 4 for height in heights[:VISIBLE_ROWS]))
        listing.currentItemChanged.connect(self._selected_item)
        listing.itemDoubleClicked.connect(lambda item: self.action_requested.emit("edit", *item.data(Qt.ItemDataRole.UserRole)))
        layout.addWidget(listing)
        self.lists[f"{tool}/{env_id}"] = listing
        return plate

    def _context_menu(self, listing: QListWidget, pos) -> None:
        item = listing.itemAt(pos)
        if item is None:
            return
        self.select(*item.data(Qt.ItemDataRole.UserRole))
        if self.context_requested:
            self.context_requested(*self._selection, listing.viewport().mapToGlobal(pos))

    def _selected_item(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            return
        self._selection = tuple(current.data(Qt.ItemDataRole.UserRole))
        self._sync_selection()
        self.selected.emit(*self._selection)

    def _sync_selection(self) -> None:
        for listing in self.lists.values():
            listing.blockSignals(True)
            match = next((listing.item(i) for i in range(listing.count())
                          if tuple(listing.item(i).data(Qt.ItemDataRole.UserRole)) == self._selection), None)
            listing.setCurrentItem(match)
            if match is None:
                listing.clearSelection()
            listing.blockSignals(False)
        self.details_host.setVisible(bool(self._selection[1]))

    def _relayout(self) -> None:
        columns = 2 if self.scroll.viewport().width() >= 760 else 1
        if self._columns == columns:
            return
        self._columns = columns
        for index, plate in enumerate(self.plates.values()):
            self.grid.removeWidget(plate)
            self.grid.addWidget(plate, index // columns, index % columns, Qt.AlignmentFlag.AlignTop)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1 if columns == 2 else 0)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._relayout()
        elif watched in self.lists.values() and event.type() == QEvent.Type.KeyPress:
            action = {Qt.Key.Key_Return: "edit", Qt.Key.Key_Enter: "edit", Qt.Key.Key_Delete: "delete"}.get(event.key())
            if action and self._selection[1]:
                self.request_action(action)
                return True
        return super().eventFilter(watched, event)
