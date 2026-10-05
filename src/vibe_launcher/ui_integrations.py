"""GitHub binding, the DeepSeek Harness service and quota queries.

Mixed into ``VibeLauncherWindow``; methods use the window's own state and helpers.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from . import runtime as runtime_module
from .ui_format import _value


class IntegrationActions:
    def github_settings(self) -> None:
        from .github_dialog import GitHubDialog
        project = self._by_id("presets", self._selected_project or "")
        if not project:
            self._notice("請先選取專案。")
            return
        dialog = GitHubDialog(self.store, project, self)
        dialog.operation_requested.connect(lambda kwargs: self._start_operation("github", kwargs, lambda result: self._github_result(dialog, kwargs, result)))
        dialog.binding_ready.connect(lambda binding: self._save_github_binding({**binding, "preset_id": project["id"]}))
        dialog.show()
        self._github_dialog = dialog
        dialog.start()

    def open_github_repository(self) -> None:
        project = self._by_id("presets", self._selected_project or "")
        try:
            url = runtime_module.github_url(project.get("github_repo", ""))
        except ValueError:
            self._notice("請先透過 GitHub 精靈綁定 repository。")
            return
        QDesktopServices.openUrl(QUrl(url))

    def _github_result(self, dialog, kwargs: dict, result: dict) -> None:
        from shiboken6 import isValid
        account_id = kwargs["github_account_id"]
        operation = kwargs["operation"].get("github_operation")
        if operation in {"login", "status"}:
            try:
                account = self.store.get("github_accounts", account_id)
            except KeyError:
                account = None          # removed while the check ran
            if account is not None:
                update = {"login": result.get("login", ""), "status": result.get("status", "unknown"),
                          "credential_storage": result.get("credential_storage", "unverified"), "checked_at": runtime_module.now()}
                if str(result.get("user_id", "")).isdigit():
                    update["user_id"] = str(result["user_id"])
                if result.get("status") == "authenticated" and not account.get("author_name") and not account.get("author_email")                         and result.get("noreply_email"):
                    # Commits made through Launcher should be attributed to this account from the start.
                    update.update(author_name=result.get("display_name") or result["login"], author_email=result["noreply_email"])
                self.store.save("github_accounts", {**account, **update})
                twins = [item for item in self.store.list("github_accounts") if item["id"] != account_id
                         and item.get("environment_id") == account.get("environment_id") and update["login"] and item.get("login") == update["login"]]
                if twins and dialog is None:
                    self._toast(f"「{account.get('name')}」和「{twins[0].get('name')}」是同一個 GitHub 帳號（{update['login']}）。"
                                "要登入不同帳號，請先在瀏覽器切換 GitHub 帳號。", "warn")
        if dialog is not None and isValid(dialog):
            dialog.handle_result(result)
        self.reload()

    def _save_github_binding(self, binding: dict) -> None:
        project_id = binding.pop("preset_id", "")
        project = self.store.get("presets", project_id) if project_id else {"id": self._new_id(), "name": binding.pop("name", "GitHub 專案"), "account_id": ""}
        if project_id and binding.get("github_account_id") != project.get("github_account_id", ""):
            for service in self.store.list("web_services"):
                if service.get("status") != "stopped" and service.get("environment_id") == project["environment_id"] and service.get("github_account_id", "") == project.get("github_account_id", ""):
                    self._notice("請先停止使用目前 GitHub 身分的共享 Harness 服務，再變更綁定。")
                    return
        self._save("presets", {**project, **binding})

    def harness_settings(self, account: dict | None = None) -> None:
        from .harness_dialog import HarnessDialog
        project = ({"account_id": account["id"], "environment_id": account["environment_id"], "github_account_id": ""}
                   if account else self._by_id("presets", self._selected_project or ""))
        if not project:
            self._notice("請先選取專案。")
            return
        dialog = HarnessDialog(self.store, project, self)
        dialog.operation_requested.connect(lambda kwargs: self._start_operation("harness", kwargs, lambda result: self._harness_result(dialog, kwargs, result)))
        dialog.open_requested.connect(lambda result: self._open_harness(dialog, result))
        dialog.show()
        self._harness_dialog = dialog

    def _harness_result(self, dialog, kwargs: dict, result: dict) -> None:
        from shiboken6 import isValid
        if result.get("service_key"):
            record = {key: value for key, value in result.items() if key in {"service_key", "status", "port", "local_port", "account_id", "github_account_id", "environment_id", "experimental"}}
            self.store.save("web_services", {**record, "id": result["service_key"], "tool": "deepseek-harness", "updated_at": runtime_module.now()})
            if result["status"] == "stopped":
                self.tunnels.close(result["service_key"])
        elif result.get("status") in {"failed", "unknown"}:
            key = runtime_module.harness_identity({"account": self.store.get("accounts", kwargs["account_id"]), "environment_id": kwargs["environment_id"],
                                                   "github_account": self.store.get("github_accounts", kwargs["github_account_id"]) if kwargs.get("github_account_id") else None})
            try:
                service = self.store.get("web_services", key)
            except KeyError:
                service = None
            if service:
                self.store.save("web_services", {**service, "status": "unknown", "updated_at": runtime_module.now()})
        if isValid(dialog):
            dialog.handle_result(result)

    def _open_harness(self, dialog, result: dict) -> None:
        if result.get("status") != "running":
            self._notice("請先確認遠端服務正在執行，再開啟連線。")
            return
        try:
            url = self.tunnels.open(result["service_key"], self.store.get("environments", result["environment_id"]), result["port"], result["local_port"])
            QDesktopServices.openUrl(QUrl(url))
            if hasattr(dialog, "set_local_status"):
                dialog.set_local_status("connected")
        except Exception as exc:
            self._error("無法連接 Harness", exc)
            if hasattr(dialog, "set_local_status"):
                dialog.set_local_status("disconnected")

    def _quota_report(self, account_id: str, environment_id: str) -> dict[str, Any] | None:
        try:
            data = json.loads(runtime_module.read_private(runtime_module.quota_file(self.store.path.parent, account_id, environment_id)))
        except (OSError, ValueError, KeyError):
            return None
        return data if isinstance(data, dict) else None

    def _refresh_quota_panel(self) -> None:
        panel = getattr(self, "quota_panel", None)
        if panel is None:
            return
        account = self._selected_record("accounts") or None
        if not account:
            panel.show_account(None, [], None)
            return
        environments = sorted(self._records.get("environments", []), key=lambda e: (e.get("id") != "local", _value(e, "name").casefold()))
        if account.get("environment_id") and account.get("auth") == "subscription":
            environments = [e for e in environments if e.get("id") == account["environment_id"]] or environments
        environment = panel.selected_environment()
        if environment not in {e.get("id") for e in environments}:
            environment = environments[0]["id"] if environments else "local"
        key = (account["id"], environment)
        panel.show_account(account, environments, self._quota_report(*key), busy=key in self._quota_busy,
                           note=self._quota_notes.get(key, ""), preferred_environment=environment)

    def query_quota(self, account_id: str, environment_id: str) -> None:
        key = (account_id, environment_id)
        account, environment = self._by_id("accounts", account_id), self._by_id("environments", environment_id)
        if key in self._quota_busy or not account or not runtime_module.quota_supported(account):
            return
        root = self.store.path.parent
        path = runtime_module.quota_file(root, account_id, environment_id)
        try:
            baseline = path.stat().st_mtime if path.exists() else 0.0
        except OSError:
            baseline = 0.0
        self._quota_notes.pop(key, None)
        if environment.get("kind") == "ssh":
            # The login lives on that host and SSH may need to ask for a password, so this goes through a terminal.
            if self._invoke("quota", account_id=account_id, environment_id=environment_id):
                self._quota_busy[key] = {"since": time.monotonic(), "baseline": baseline, "path": path, "ttl": 180}
        else:
            self._quota_busy[key] = {"since": time.monotonic(), "baseline": baseline, "path": None, "ttl": 90}

            def work() -> None:
                error = ""
                try:
                    summary = runtime_module.fetch_quota(account, root, environment_id)
                    runtime_module.write_json(path, summary)
                except Exception as exc:
                    error = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
                self._quota_done.emit(account_id, environment_id, error)

            threading.Thread(target=work, daemon=True).start()
        self._refresh_quota_panel()

    def _on_quota_done(self, account_id: str, environment_id: str, error: str) -> None:
        key = (account_id, environment_id)
        self._quota_busy.pop(key, None)
        if error:
            lowered = error.lower()
            message = ("這個帳號在該環境尚未登入。請先按「登入…」完成登入後再查詢。" if "authentication" in lowered or "login" in lowered
                       else "找不到 Codex CLI，請先在該環境安裝。" if "executable is unavailable" in lowered
                       else f"無法取得配額：{error}")
            self._quota_notes[key] = message
            self._toast(message, "warn")
        else:
            self._quota_notes.pop(key, None)
            self._toast("配額已更新。")
        self._refresh_quota_panel()

    def _poll_quota(self) -> None:
        for key, info in list(self._quota_busy.items()):
            path = info["path"]
            try:
                finished = path is not None and path.exists() and path.stat().st_mtime > info["baseline"]
            except OSError:
                finished = False
            if finished:
                self._on_quota_done(key[0], key[1], "")
            elif time.monotonic() - info["since"] > info["ttl"]:
                self._quota_notes[key] = "查詢沒有完成。請檢查終端輸出（尚未登入、SSH 驗證未完成，或該主機沒有 Codex）。"
                self._quota_busy.pop(key, None)
                self._refresh_quota_panel()
