"""Project GitHub binding: which account pushes, and whose name goes on the commits.

Three steps on one page: GitHub account -> commit identity -> repository. Official Git/gh operations run in the
launcher runner; this dialog only collects choices and shows their results. Repository files are changed only after
a preview of the exact Git settings has been confirmed.
"""
from __future__ import annotations

from .i18n import tr

from .i18n import tr

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QScrollArea, QVBoxLayout, QWidget)

from . import runtime, theme
from .drafts import FormDraft
from .kit import label, make_button
from .theme import S

NO_REMOTE = "-"      # remote choices that are not remote names (Git names cannot start with these)
NEW_REMOTE = "+"
DEFAULT_NAME = tr('github_dialog.bc3a0bab5dfe')
BROWSER_HINT = (tr('github_dialog.7d6187ee3e22'))


def _card(title: str, subtitle: str = "") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setProperty("surface", "card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(S.md, S.md, S.md, S.md)
    layout.setSpacing(S.sm)
    layout.addWidget(label(title, "section"))
    if subtitle:
        layout.addWidget(label(subtitle, "meta", wrap=True))
    return frame, layout


class GitHubDialog(QDialog):
    operation_requested = Signal(dict)
    binding_ready = Signal(dict)

    def __init__(self, store, project: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = store
        self.project = dict(project)
        self.environment_id = self.project.get("environment_id") or "local"
        self._info: dict = {}
        self._identity: dict = {}           # login / noreply email of the selected account, from the last check
        self._preview: dict = {}
        self._preview_request: dict = {}
        self._busy = False
        self._pending = ""
        self._fresh = ""                    # account record created by "登入新帳號", until its login is known
        self.setWindowTitle(tr('github_dialog.441c62704d39'))
        self.resize(640, 720)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        environment = next((e for e in store.list("environments") if e["id"] == self.environment_id), {})

        root = QVBoxLayout(self)
        root.setContentsMargins(S.lg, S.lg, S.lg, S.md)
        root.setSpacing(S.md)
        root.addWidget(label(f"GitHub · {self.project.get('name', '')}", "section"))
        root.addWidget(label(f"{environment.get('name', self.environment_id)} · {self.project.get('path', '')}", "meta", wrap=True))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(S.md)

        card, box = _card(tr('github_dialog.648991efab41'), tr('github_dialog.ea1125d53d99'))
        self.account = QComboBox()
        self.account.setAccessibleName(tr('github_dialog.bc3a0bab5dfe'))
        box.addWidget(self.account)
        row = QHBoxLayout()
        self.login_button = make_button(tr('github_dialog.1b5e12c1f832'), small=True, callback=lambda: self._request("login"))
        self.new_button = make_button(tr('github_dialog.929ac76e998a'), "secondary", "plus", small=True, callback=self._new_account)
        self.status_button = make_button(tr('github_dialog.cb3cd0dff7fc'), "ghost", "refresh", small=True, callback=lambda: self._request("status"))
        for button in (self.new_button, self.login_button, self.status_button):
            row.addWidget(button)
        row.addStretch(1)
        box.addLayout(row)
        self.auth_status = label("", wrap=True)
        box.addWidget(self.auth_status)
        box.addWidget(label(BROWSER_HINT, "meta", wrap=True))
        layout.addWidget(card)

        card, box = _card(tr('github_dialog.aa042cd237c2'), tr('github_dialog.c9fb8fa9b032'))
        form = QFormLayout()
        form.setSpacing(S.sm)
        self.author_name = QLineEdit()
        self.author_name.setPlaceholderText(tr('github_dialog.706b1c80143c'))
        self.author_email = QLineEdit()
        self.author_email.setPlaceholderText(tr('github_dialog.c39c77ed9add'))
        form.addRow(tr('github_dialog.a5274f381305'), self.author_name)
        form.addRow("Email", self.author_email)
        box.addLayout(form)
        self.noreply_button = make_button(tr('github_dialog.6b46c08e5fa8'), "ghost", small=True, callback=self._use_noreply)
        box.addWidget(self.noreply_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(card)

        card, box = _card("3  Repository", tr('github_dialog.98f7fdfbfedf'))
        self.repo_status = label(tr('github_dialog.f92612d11856'), wrap=True)
        box.addWidget(self.repo_status)
        self.init_button = make_button(tr('github_dialog.161d922204ac'), small=True, callback=lambda: self._request("init"))
        self.init_button.hide()
        box.addWidget(self.init_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.remote = QComboBox()
        self.remote.setAccessibleName("Repository remote")
        box.addWidget(self.remote)
        self.remote_url = QLineEdit()
        self.remote_url.setPlaceholderText("https://github.com/owner/repository")
        self.remote_url.setAccessibleName(tr('github_dialog.3af67ab2ef87'))
        self.remote_url.hide()
        box.addWidget(self.remote_url)
        self.remote_note = label("", "meta", wrap=True)
        box.addWidget(self.remote_note)
        self.write_repo = QCheckBox(tr('github_dialog.780278b12439'))
        self.write_repo.setChecked(True)
        box.addWidget(self.write_repo)
        box.addWidget(label(tr('github_dialog.78e241e01bda'), "meta", wrap=True))
        layout.addWidget(card)

        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setMinimumHeight(140)
        self.diff.setAccessibleName(tr('github_dialog.fdbd0a945876'))
        self.diff.hide()
        layout.addWidget(self.diff)
        layout.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        self.message = label("", wrap=True)
        root.addWidget(self.message)
        buttons = QHBoxLayout()
        self.unbind_button = make_button(tr('github_dialog.6e70bd533dfb'), "ghost", callback=self._unbind)
        self.unbind_button.setVisible(bool(self.project.get("github_account_id")))
        buttons.addWidget(self.unbind_button)
        buttons.addStretch(1)
        buttons.addWidget(make_button(tr('github_dialog.c7fdddf79eaa'), "ghost", callback=self.close))
        self.save_button = make_button(tr('github_dialog.64cef98c3a15'), "primary", callback=self._save)
        buttons.addWidget(self.save_button)
        root.addLayout(buttons)

        self._inputs = [self.account, self.author_name, self.author_email, self.remote, self.remote_url, self.write_repo]
        self.account.currentIndexChanged.connect(self._account_changed)
        self.remote.currentIndexChanged.connect(self._remote_changed)
        for field in (self.author_name, self.author_email, self.remote_url):
            field.textEdited.connect(self._invalidate_preview)
        self.write_repo.toggled.connect(self._invalidate_preview)
        self._load_accounts(self.project.get("github_account_id", ""))
        self.draft = FormDraft(self, store.path.parent, f"github:{self.project.get('id', '')}",
                               {"author_name": self.author_name, "author_email": self.author_email,
                                "remote_url": self.remote_url, "write_repo": self.write_repo})
        for field in self.findChildren(QLabel):
            field.setTextFormat(Qt.TextFormat.PlainText)
        self._buttons()

    def showEvent(self, event):
        super().showEvent(event)
        theme.apply_dark_titlebar(self)

    def start(self) -> None:
        """First look: the selected account's login, then the repository (called once signals are connected)."""
        if self.account.currentData():
            self._request("status")
        else:
            self.auth_status.setText(tr('github_dialog.69a8ff78e634'))

    # -- accounts ------------------------------------------------------------------------------
    def _accounts(self) -> list[dict]:
        return [item for item in self.store.list("github_accounts") if item["environment_id"] == self.environment_id]

    def _account_record(self) -> dict:
        return next((item for item in self._accounts() if item["id"] == self.account.currentData()), {})

    def _load_accounts(self, selected=""):
        self.account.blockSignals(True)
        self.account.clear()
        for item in self._accounts():
            self.account.addItem(item["name"] + (" · " + item["login"] if item.get("login") and item["login"] != item["name"] else ""),
                                 item["id"])
        index = self.account.findData(selected)
        self.account.setCurrentIndex(index if index >= 0 else 0)
        self.account.blockSignals(False)
        self._account_changed()

    def _account_changed(self, *_):
        record = self._account_record()
        self._identity = {}
        self.unbind_button.setProperty("confirmed", False)
        self.unbind_button.setText(tr('github_dialog.6e70bd533dfb'))
        self.author_name.setText(record.get("author_name", ""))
        self.author_email.setText(record.get("author_email", ""))
        if record:
            login = record.get("login")
            self.auth_status.setText(tr('github_dialog.e54605ed3733', v0=f'{login}') if login else tr('github_dialog.992293f69d3c'))
        self._invalidate_preview()

    def _new_account(self):
        names = {item["name"] for item in self._accounts()}
        name = next(f"{DEFAULT_NAME} {n}" for n in range(1, 1000) if f"{DEFAULT_NAME} {n}" not in names)
        try:
            item = self.store.save("github_accounts", {"name": name, "environment_id": self.environment_id})
        except (ValueError, OSError) as error:
            self.message.setText(str(error))
            return
        self._fresh = item["id"]
        self._load_accounts(item["id"])
        self._request("login")

    def _settle_fresh(self, result: dict) -> bool:
        """A just-created record keeps the login it got, or disappears when it duplicates one or got none.

        Returns True when the record was removed (the dialog then shows why instead of the login result)."""
        fresh, self._fresh = self._fresh, ""
        if not fresh or fresh != self.account.currentData():
            return False
        login = result.get("login", "") if result.get("status") == "authenticated" else ""
        twin = next((item for item in self._accounts() if item["id"] != fresh and login and item.get("login") == login), None)
        if not login or twin:
            try:
                self.store.delete("github_accounts", fresh)
            except (ValueError, OSError, KeyError):
                return False
            self._load_accounts(twin["id"] if twin else self.project.get("github_account_id", ""))
            if twin:
                self._identity = {key: result.get(key, "") for key in ("login", "user_id", "display_name", "noreply_email")}
                self.auth_status.setText(tr('github_dialog.5d2ea35cd779', v0=f'{login}'))
            self.message.setText(tr('github_dialog.d3e878bb708d', v0=f'{login}', v1=f"{twin['name']}")
                                 if twin else tr('github_dialog.abb4c0806fb5'))
            return True
        record = self._account_record()
        if record.get("name", "").startswith(DEFAULT_NAME):
            self.store.save("github_accounts", {**record, "name": login})
            self._load_accounts(fresh)
        return False

    def _use_noreply(self):
        if not self._identity.get("noreply_email"):
            self.message.setText(tr('github_dialog.cd587ee41a88'))
            return
        self.author_email.setText(self._identity["noreply_email"])
        if not self.author_name.text().strip():
            self.author_name.setText(self._identity.get("display_name") or self._identity["login"])
        self._invalidate_preview()

    # -- repository ----------------------------------------------------------------------------
    def _selected_remote(self) -> dict:
        return next((item for item in self._info.get("remotes", []) if item["name"] == self.remote.currentData()), {})

    def _fill_remotes(self, previous: str) -> None:
        names = [item["name"] for item in self._info.get("remotes", [])]
        self.remote.blockSignals(True)
        self.remote.clear()
        for item in self._info.get("remotes", []):
            self.remote.addItem(f"{item['name']} → {item['url']}", item["name"])
        self.remote.addItem(tr('github_dialog.be4309ed37bd'), NO_REMOTE)
        if self._info.get("git") and "origin" not in names:
            self.remote.addItem(tr('github_dialog.f1bc6d4d4930'), NEW_REMOTE + "origin")
        for choice in (previous, "origin", names[0] if names else NO_REMOTE, NO_REMOTE):
            index = self.remote.findData(choice)
            if choice and index >= 0:
                self.remote.setCurrentIndex(index)
                break
        self.remote.blockSignals(False)
        self._remote_changed()

    def _remote_changed(self, *_):
        choice = self.remote.currentData() or ""
        self.remote_url.setVisible(choice.startswith(NEW_REMOTE))
        url = self._selected_remote().get("url", "")
        self.remote_note.setText(
            tr('github_dialog.11afb6da9779')
            if url.startswith(("git@", "ssh://")) else
            tr('github_dialog.063ef35e45b9') if url.startswith("[") else
            tr('github_dialog.aaa3321371de') if choice.startswith(NEW_REMOTE) else "")
        self._invalidate_preview()

    def _show_repository(self, result: dict) -> None:
        self._info = result
        git = bool(result.get("git"))
        self.init_button.setVisible(not git)
        self.write_repo.setEnabled(git)
        if git:
            remotes = len(result.get("remotes", []))
            self.repo_status.setText(f"Git repository · branch {result.get('branch') or tr('github_dialog.40a6cd09c6fa')} · "
                                     + (tr('github_dialog.ece927ea35a3', v0=f'{remotes}') if remotes else tr('github_dialog.8a9723347cdc')))
        else:
            self.repo_status.setText(tr('github_dialog.b5302d4f92c2'))
        self._fill_remotes(self.project.get("github_remote", ""))

    # -- state ---------------------------------------------------------------------------------
    def _buttons(self):
        idle = not self._busy
        has_account = bool(self.account.currentData())
        for button in (self.login_button, self.status_button):
            button.setEnabled(idle and has_account)
        self.new_button.setEnabled(idle)
        self.noreply_button.setEnabled(idle and bool(self._identity))
        self.init_button.setEnabled(idle)
        self.save_button.setEnabled(idle and bool(self._identity) and bool(self._info))
        self.save_button.setText(tr('github_dialog.ec563e31f6da') if self._preview else tr('github_dialog.64cef98c3a15'))
        self.unbind_button.setEnabled(idle)
        for field in self._inputs:
            field.setEnabled(idle and (field is not self.write_repo or bool(self._info.get("git"))))

    def _invalidate_preview(self, *_):
        self._preview = {}
        self.diff.clear()
        self.diff.hide()
        self._buttons()

    def _request(self, operation, **fields):
        if self._busy or not self.account.currentData():
            return
        self._pending = operation
        self._busy = True
        if operation in {"login", "status"}:
            self._identity = {}
        self._buttons()
        self.message.setText(tr('github_dialog.1982bbe8d00f') if operation == "login" else tr('github_dialog.9a39bc06648c'))
        payload = {"github_operation": operation, "path": self.project.get("path", ""), **fields}
        account = self.account.currentData()
        if operation == "unbind":
            account = self.project.get("github_account_id") or account    # the record that wrote the settings
        self.operation_requested.emit({"github_account_id": account, "environment_id": self.environment_id,
                                       "operation": payload})

    # -- save ----------------------------------------------------------------------------------
    def _author(self) -> tuple[str, str]:
        name, email = runtime.github_author({"author_name": self.author_name.text().strip(),
                                             "author_email": self.author_email.text().strip()})
        if not name or not email:
            raise ValueError(tr('github_dialog.f251418f55d7'))
        return name, email

    def _binding_fields(self) -> dict:
        choice = self.remote.currentData() or NO_REMOTE
        name, email = self._author()
        author = {"github_author_name": name, "github_author_email": email}
        if choice.startswith(NEW_REMOTE):
            return {"github_remote": choice[len(NEW_REMOTE):], "github_new_remote": True,
                    "github_url": runtime.github_url(self.remote_url.text().strip()), **author}
        if choice == NO_REMOTE:
            return {"github_remote": "", **author}
        selected = self._selected_remote()
        return {"github_remote": selected["name"], "github_expected_remote_url": selected["url"],
                "github_url": runtime.github_repository_url(selected["url"]), **author}

    def _writes_repository(self) -> bool:
        return bool(self._info.get("git")) and (self.write_repo.isChecked() or (self.remote.currentData() or "").startswith(NEW_REMOTE))

    def _save(self):
        if not self._scope_available():
            return
        try:
            name, email = self._author()
            fields = self._binding_fields()
        except (ValueError, KeyError) as error:
            self.message.setText(str(error))
            return
        record = self._account_record()
        if (record.get("author_name"), record.get("author_email")) != (name, email):
            try:
                self.store.save("github_accounts", {**record, "author_name": name, "author_email": email})
            except (ValueError, OSError) as error:
                self.message.setText(str(error))
                return
        if not self._writes_repository():
            self._associate()
        elif self._preview:
            self._request("bind", **self._preview_request, github_apply=True, github_revision=self._preview["revision"])
        else:
            self._preview_request = fields
            self._request("bind", **fields)

    def _associate(self):
        selected = self._selected_remote()
        choice = self.remote.currentData() or NO_REMOTE
        url = ""
        if choice.startswith(NEW_REMOTE):
            url = runtime.github_url(self.remote_url.text().strip())
        elif selected:
            url = runtime.github_repository_url(selected["url"])
        binding = {"github_account_id": self.account.currentData(), "github_repo": url,
                   "github_remote": selected.get("name", "") or (choice[len(NEW_REMOTE):] if choice.startswith(NEW_REMOTE) else ""),
                   "github_branch": self._info.get("branch", ""), "github_access": "unchecked"}
        self.binding_ready.emit(binding)
        self.project.update(binding)
        self.unbind_button.show()
        self.draft.clear()
        login = self._identity.get("login", "")
        self.message.setText(tr('github_dialog.ff911eedd506', v0=f'{login}', v1=f'{self.author_name.text().strip()}')
                             + (tr('github_dialog.76a2ae99e18b') if self._writes_repository() else "。")
                             + ("" if url else tr('github_dialog.b464a94562b6')))

    def _unbind(self):
        if not self._scope_available(unbind=True):
            return
        if self.unbind_button.property("confirmed"):
            self.unbind_button.setProperty("confirmed", False)
            self.unbind_button.setText(tr('github_dialog.6e70bd533dfb'))
            self._request("unbind")
        else:
            self.unbind_button.setProperty("confirmed", True)
            self.unbind_button.setText(tr('github_dialog.d8e4f660743e'))
            self.message.setText(tr('github_dialog.577b51b3a848'))

    def _scope_available(self, *, unbind=False):
        previous = self.project.get("github_account_id", "")
        selected = self.account.currentData() or ""
        if previous == selected and not unbind:
            return True
        for service in self.store.list("web_services"):
            if (service.get("environment_id") == self.environment_id
                    and service.get("github_account_id", "") in {previous, selected}
                    and service.get("status") != "stopped"):
                self.message.setText(tr('github_dialog.d5303c8af104'))
                return False
        return True

    # -- results -------------------------------------------------------------------------------
    def handle_result(self, result: dict):
        operation = self._pending
        self._pending = ""
        self._busy = False
        invalid = ((operation == "bind" and result.get("status") not in {"preview", "bound"})
                   or (operation == "unbind" and result.get("status") != "unbound")
                   or (operation in {"inspect", "init"} and not result.get("root")))
        failed = bool(result.get("error")) or result.get("status") in {"failed", "unknown"} or invalid
        if operation in {"login", "status"} and self._fresh and self._settle_fresh(result if not failed else {}):
            if self._identity and not self._info:
                explanation = self.message.text()
                self._request("inspect")
                self.message.setText(explanation)
                return
            self._buttons()
            return
        if failed or (operation in {"login", "status"} and result.get("status") != "authenticated"):
            if operation == "bind":
                self._invalidate_preview()
            if operation in {"login", "status"} and not failed:
                self.auth_status.setText(tr('github_dialog.7600e663ea0f'))
                self.message.setText("")
            else:
                self.message.setText(result.get("error") or tr('github_dialog.960b534a1de5'))
            self._buttons()
            return
        self.message.setText("")
        if operation in {"login", "status"}:
            self._identity = {key: result.get(key, "") for key in ("login", "user_id", "display_name", "noreply_email")}
            twin = next((item for item in self._accounts() if item["id"] != self.account.currentData()
                         and item.get("login") == self._identity["login"]), None)
            self.auth_status.setText(tr('github_dialog.5d2ea35cd779', v0=f"{self._identity['login']}")
                                     + (tr('github_dialog.33a261a07ec6', v0=f"{twin['name']}") if twin else ""))
            if not self.author_name.text().strip() and not self.author_email.text().strip():
                self._use_noreply()
            if not self._info:
                self._busy = False
                self._buttons()
                self._request("inspect")
                return
        elif operation in {"inspect", "init"}:
            self._show_repository(result)
            if operation == "init":
                self.message.setText(tr('github_dialog.83aad41d53f1'))
        elif operation == "bind" and result.get("status") == "preview":
            self._preview = result
            rows = [tr('github_dialog.521835d678e4', v0=f'{key}', v1=f"{entry['before'] or tr('github_dialog.319a5e04ebb0')}", v2=f"{entry['after']}")
                    for key, entry in result.get("changes", {}).items()]
            self.diff.setPlainText(tr('github_dialog.3925fa3dd69a') + "\n\n".join(rows))
            self.diff.show()
            self.message.setText(tr('github_dialog.bd956b81f28c'))
        elif operation == "bind" and result.get("status") == "bound":
            remotes = self._info.setdefault("remotes", [])
            for item in remotes:
                if item["name"] == result["remote"]:
                    item["url"] = result["url"]
            if result["remote"] and all(item["name"] != result["remote"] for item in remotes):
                remotes.append({"name": result["remote"], "url": result["url"]})   # the origin just added
            self._fill_remotes(result["remote"] or NO_REMOTE)
            self._associate()
            self._invalidate_preview()
        elif operation == "unbind":
            self.binding_ready.emit({"github_account_id": "", "github_repo": "", "github_remote": "", "github_branch": "", "github_access": ""})
            self.project.update(github_account_id="", github_remote="")
            self.unbind_button.hide()
            kept = "、".join(result.get("preserved", []))
            self.message.setText(tr('github_dialog.56227534a638') + (tr('github_dialog.1c8910910646', v0=f'{kept}') if kept else ""))
        self._buttons()
