"""Project GitHub binding: which account pushes, and whose name goes on the commits.

Three steps on one page: GitHub account -> commit identity -> repository. Official Git/gh operations run in the
launcher runner; this dialog only collects choices and shows their results. Repository files are changed only after
a preview of the exact Git settings has been confirmed.
"""
from __future__ import annotations

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QScrollArea, QVBoxLayout, QWidget)

from . import runtime, theme
from .drafts import FormDraft
from .kit import label, make_button
from .theme import S

NO_REMOTE = "-"      # remote choices that are not remote names (Git names cannot start with these)
NEW_REMOTE = "+"
DEFAULT_NAME = "GitHub 帳號"
BROWSER_HINT = ("登入時，瀏覽器會用「目前登入 github.com 的帳號」授權。要登入另一個帳號，請先在 github.com 右上角切換帳號，"
                "或把終端顯示的網址貼到無痕視窗。")


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
        self.setWindowTitle("GitHub 綁定")
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

        card, box = _card("1  GitHub 帳號", "push／pull 與 gh 指令使用這個帳號；登入由官方 gh 處理，Launcher 不接收 token。")
        self.account = QComboBox()
        self.account.setAccessibleName("GitHub 帳號")
        box.addWidget(self.account)
        row = QHBoxLayout()
        self.login_button = make_button("重新登入此帳號…", small=True, callback=lambda: self._request("login"))
        self.new_button = make_button("登入新帳號…", "secondary", "plus", small=True, callback=self._new_account)
        self.status_button = make_button("重新檢查", "ghost", "refresh", small=True, callback=lambda: self._request("status"))
        for button in (self.new_button, self.login_button, self.status_button):
            row.addWidget(button)
        row.addStretch(1)
        box.addLayout(row)
        self.auth_status = label("", wrap=True)
        box.addWidget(self.auth_status)
        box.addWidget(label(BROWSER_HINT, "meta", wrap=True))
        layout.addWidget(card)

        card, box = _card("2  Commit 身分", "存在這個 GitHub 帳號上。Launcher 開啟的 Codex／Claude 在綁定它的專案 commit 時，一律用這個名稱與 email。")
        form = QFormLayout()
        form.setSpacing(S.sm)
        self.author_name = QLineEdit()
        self.author_name.setPlaceholderText("例如：user")
        self.author_email = QLineEdit()
        self.author_email.setPlaceholderText("建議用 GitHub noreply email")
        form.addRow("名稱", self.author_name)
        form.addRow("Email", self.author_email)
        box.addLayout(form)
        self.noreply_button = make_button("使用此帳號的 GitHub noreply email", "ghost", small=True, callback=self._use_noreply)
        box.addWidget(self.noreply_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(card)

        card, box = _card("3  Repository", "remote 可以留空，之後再回來補。")
        self.repo_status = label("讀取中…", wrap=True)
        box.addWidget(self.repo_status)
        self.init_button = make_button("在此資料夾執行 git init", small=True, callback=lambda: self._request("init"))
        self.init_button.hide()
        box.addWidget(self.init_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.remote = QComboBox()
        self.remote.setAccessibleName("Repository remote")
        box.addWidget(self.remote)
        self.remote_url = QLineEdit()
        self.remote_url.setPlaceholderText("https://github.com/owner/repository")
        self.remote_url.setAccessibleName("新 origin 的 GitHub 網址")
        self.remote_url.hide()
        box.addWidget(self.remote_url)
        self.remote_note = label("", "meta", wrap=True)
        box.addWidget(self.remote_note)
        self.write_repo = QCheckBox("同時寫入此 repository 的 Git 設定")
        self.write_repo.setChecked(True)
        box.addWidget(self.write_repo)
        box.addWidget(label("勾選後，你在 Launcher 以外手動 commit／push 也會用這個帳號；寫入前會先列出差異。", "meta", wrap=True))
        layout.addWidget(card)

        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setMinimumHeight(140)
        self.diff.setAccessibleName("將寫入的 Git 設定")
        self.diff.hide()
        layout.addWidget(self.diff)
        layout.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        self.message = label("", wrap=True)
        root.addWidget(self.message)
        buttons = QHBoxLayout()
        self.unbind_button = make_button("解除綁定", "ghost", callback=self._unbind)
        self.unbind_button.setVisible(bool(self.project.get("github_account_id")))
        buttons.addWidget(self.unbind_button)
        buttons.addStretch(1)
        buttons.addWidget(make_button("關閉", "ghost", callback=self.close))
        self.save_button = make_button("儲存綁定", "primary", callback=self._save)
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
            self.auth_status.setText("此環境還沒有 GitHub 帳號。按「登入新帳號…」開始。")

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
        self.unbind_button.setText("解除綁定")
        self.author_name.setText(record.get("author_name", ""))
        self.author_email.setText(record.get("author_email", ""))
        if record:
            login = record.get("login")
            self.auth_status.setText(f"上次檢查：{login}（按「重新檢查」確認）" if login else "尚未確認登入。")
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
                self.auth_status.setText(f"已登入：{login}")
            self.message.setText(f"瀏覽器授權的還是「{login}」，已改選既有的「{twin['name']}」。要登入別的帳號，請先在瀏覽器切換 GitHub 帳號再試。"
                                 if twin else "沒有完成登入，未新增帳號。")
            return True
        record = self._account_record()
        if record.get("name", "").startswith(DEFAULT_NAME):
            self.store.save("github_accounts", {**record, "name": login})
            self._load_accounts(fresh)
        return False

    def _use_noreply(self):
        if not self._identity.get("noreply_email"):
            self.message.setText("請先完成登入檢查，才能取得此帳號的 noreply email。")
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
        self.remote.addItem("不設定 remote（之後可補）", NO_REMOTE)
        if self._info.get("git") and "origin" not in names:
            self.remote.addItem("新增 origin…", NEW_REMOTE + "origin")
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
            "此 remote 使用 SSH：push 會用你的 SSH key 而不是這個帳號。勾選下方寫入設定，會改成同一個 repository 的 HTTPS 網址。"
            if url.startswith(("git@", "ssh://")) else
            "此 remote 的網址不是 GitHub 或含有憑證，無法用於綁定；請先在 Git 中修正。" if url.startswith("[") else
            "新增的 origin 會在確認寫入後建立。" if choice.startswith(NEW_REMOTE) else "")
        self._invalidate_preview()

    def _show_repository(self, result: dict) -> None:
        self._info = result
        git = bool(result.get("git"))
        self.init_button.setVisible(not git)
        self.write_repo.setEnabled(git)
        if git:
            remotes = len(result.get("remotes", []))
            self.repo_status.setText(f"Git repository · branch {result.get('branch') or '（尚無 commit）'} · "
                                     + (f"{remotes} 個 remote" if remotes else "尚無 remote"))
        else:
            self.repo_status.setText("這個資料夾還不是 Git repository。可以先只綁帳號，或按下方按鈕 git init。")
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
        self.save_button.setText("確認寫入並綁定" if self._preview else "儲存綁定")
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
        self.message.setText("請在官方 gh 終端與瀏覽器完成登入。" if operation == "login" else "執行中…")
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
            raise ValueError("請填寫 commit 名稱與 email（可按「使用此帳號的 GitHub noreply email」）。")
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
        self.message.setText(f"已綁定 {login}。之後 Launcher 在這個專案開啟的 agent 會以 {self.author_name.text().strip()} commit"
                             + ("，並寫入了 repository 設定。" if self._writes_repository() else "。")
                             + ("" if url else " remote 尚未設定，之後可回來補。"))

    def _unbind(self):
        if not self._scope_available(unbind=True):
            return
        if self.unbind_button.property("confirmed"):
            self.unbind_button.setProperty("confirmed", False)
            self.unbind_button.setText("解除綁定")
            self._request("unbind")
        else:
            self.unbind_button.setProperty("confirmed", True)
            self.unbind_button.setText("確認解除")
            self.message.setText("會移除這個專案的 GitHub 綁定，並把 Launcher 寫入的 repository 設定還原；你之後自己改過的設定會保留。再按一次確認。")

    def _scope_available(self, *, unbind=False):
        previous = self.project.get("github_account_id", "")
        selected = self.account.currentData() or ""
        if previous == selected and not unbind:
            return True
        for service in self.store.list("web_services"):
            if (service.get("environment_id") == self.environment_id
                    and service.get("github_account_id", "") in {previous, selected}
                    and service.get("status") != "stopped"):
                self.message.setText("請先停止此環境與 GitHub 身分的共享 Harness 服務，再變更綁定。未知狀態也需要先檢查。")
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
                self.auth_status.setText("此帳號尚未登入。按「重新登入此帳號…」。")
                self.message.setText("")
            else:
                self.message.setText(result.get("error") or "操作狀態不明或不可用；請檢查連線／登入後重試，既有綁定保留。")
            self._buttons()
            return
        self.message.setText("")
        if operation in {"login", "status"}:
            self._identity = {key: result.get(key, "") for key in ("login", "user_id", "display_name", "noreply_email")}
            twin = next((item for item in self._accounts() if item["id"] != self.account.currentData()
                         and item.get("login") == self._identity["login"]), None)
            self.auth_status.setText(f"已登入：{self._identity['login']}"
                                     + (f"\n注意：「{twin['name']}」也是這個 GitHub 帳號。要用不同帳號，請在瀏覽器切換後重新登入。" if twin else ""))
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
                self.message.setText("已建立 Git repository。")
        elif operation == "bind" and result.get("status") == "preview":
            self._preview = result
            rows = [f"{key}\n  原值：{entry['before'] or '(未設定)'}\n  寫入：{entry['after']}"
                    for key, entry in result.get("changes", {}).items()]
            self.diff.setPlainText("將寫入 .git/config（不改全域設定）：\n\n" + "\n\n".join(rows))
            self.diff.show()
            self.message.setText("確認以下差異後，再按「確認寫入並綁定」。")
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
            self.message.setText("已解除綁定。" + (f"你自己改過的設定已保留：{kept}" if kept else ""))
        self._buttons()
