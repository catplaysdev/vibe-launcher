"""Sessions page: listing, bulk stop/remove/clean and status checks of recorded work.

Mixed into ``VibeLauncherWindow``; methods use the window's own state and helpers.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt

from . import env_state
from . import runtime as runtime_module
from .theme import TOOL_ICONS
from .ui_format import TONE_TOKENS, _display_time, _value


class SessionActions:
    def _render_sessions(self) -> None:
        if not hasattr(self, "sessions_list"):
            return
        rows = []
        for session in sorted(self._records.get("sessions", []), key=lambda s: _value(s, "updated_at"), reverse=True):
            preset = self._by_id("presets", _value(session, "preset_id"))
            label = f"{_value(session, 'name') or _value(preset, 'name', '工作階段')}"
            account = self._by_id("accounts", _value(session, "account_id"))
            environment = self._by_id("environments", _value(session, "environment_id"))
            label += (f"\n{_value(environment, 'name')} · {_value(account, 'name')} · 更新於 {_display_time(_value(session, 'updated_at'))}"
                      f"\n{_value(session, 'path')}")
            badge = env_state.session_badge(_value(session, "status", "unknown"))
            tool = "shell" if _value(session, "mode") == "shell" else _value(account, "tool", "shell")
            rows.append((label, session.get("id"), _value(session, "path"), badge, (TOOL_ICONS.get(tool, "terminal"), TONE_TOKENS.get(badge[1], "neutral"))))
        self._fill_list(self.sessions_list, rows)
        self.sessions_panel.show_empty(not rows)
        self._render_session_summary()
        self._sync_session_actions()
        auth = self._records.get("auth_states", [])
        configured = len(self._records.get("accounts", []))
        auth_count = sum(1 for state in auth if _value(state, "status").casefold() in {"authenticated", "logged_in", "ready", "ok"})
        self.auth_summary.setText(f"帳號 {configured} 個 · 已驗證環境狀態 {auth_count} 筆 · 設定完成與登入驗證會分開顯示")

    def session_actions(self, session: dict[str, Any]) -> tuple[bool, bool, bool, str]:
        """(can attach, can stop, can remove, explanation) for a session, from what we actually know about it."""
        if not session:
            return False, False, False, "選一筆工作階段查看說明。工作階段是專案每次啟動後留下的紀錄。"
        status = _value(session, "status", "unknown")
        local = self._by_id("environments", _value(session, "environment_id")).get("kind") == "local"
        persistent = session.get("persistent", True) is not False
        live = status in {"connecting", "waiting_auth", "running", "detached", "unknown"}
        can_attach = live and not local and persistent and status in {"running", "detached", "unknown"}
        can_stop = live
        can_remove = status in {"ended", "failed", "unknown"}
        if status in {"running", "detached"} and local:
            hint = "本機工作在原本的終端視窗中執行；關閉該視窗會結束它。"
        elif status in {"running", "detached"} and persistent:
            hint = "工作在伺服器的 tmux 中運行。可以關閉終端，之後按「接回終端」繼續；「停止工作階段」會結束它。"
        elif status in {"running", "detached"}:
            hint = "這個工作沒有使用 tmux：關閉終端就會結束，無法接回。"
        elif status == "connecting":
            hint = "終端已開啟，正在連線或驗證。互動期間這裡不會即時更新，離開後才會顯示結果。"
        elif status == "waiting_auth":
            hint = "正在等待登入驗證。請到終端或瀏覽器完成。"
        elif status == "unknown":
            hint = "無法確認遠端狀態（例如終端被直接關閉）。按「更新工作狀態」重新查詢，可能需要再次驗證。"
        elif status == "ended":
            hint = "已停止。可以移除本機紀錄，或回到專案頁重新啟動。"
        elif status == "failed":
            hint = "啟動或執行失敗。回到終端輸出看原因，修正後重新啟動；也可以移除這筆紀錄。"
        else:
            hint = ""
        if status in {"running", "detached", "unknown"} and not local and not persistent:
            can_attach = False
        return can_attach, can_stop, can_remove, hint

    def _selected_sessions(self) -> list[dict[str, Any]]:
        listing = getattr(self, "sessions_list", None)
        found = [self._by_id("sessions", i.data(Qt.ItemDataRole.UserRole)) for i in listing.selectedItems()] if listing else []
        found = [s for s in found if s]
        if not found:
            current = self._selected_record("sessions") if listing else {}
            found = [current] if current else []
        return found

    def _session_counts(self) -> dict[str, int]:
        sessions = self._records.get("sessions", [])
        status = [_value(s, "status", "unknown") for s in sessions]
        return {"total": len(sessions), "unknown": status.count("unknown"), "finished": sum(1 for s in status if s in {"ended", "failed"}),
                "live": sum(1 for s in status if s in {"connecting", "waiting_auth", "running", "detached"})}

    def _render_session_summary(self) -> None:
        if not hasattr(self, "session_summary"):
            return
        counts = self._session_counts()
        self.session_summary.setText(f"共 {counts['total']} 筆 · 進行中 {counts['live']} · 狀態不明 {counts['unknown']} · 已結束／失敗 {counts['finished']}")
        self.session_check_button.setEnabled(counts["unknown"] > 0)
        self.session_check_button.setText(f"檢查狀態不明的（{counts['unknown']}）" if counts["unknown"] else "檢查狀態不明的")
        self.session_clean_button.setEnabled(counts["finished"] > 0)
        self.session_clean_button.setText(f"清除已結束的（{counts['finished']}）" if counts["finished"] else "清除已結束的")

    def _sync_session_actions(self) -> None:
        if not hasattr(self, "session_attach_button"):
            return
        chosen = self._selected_sessions()
        if len(chosen) > 1:
            states = [self.session_actions(s) for s in chosen]
            stoppable = sum(1 for s in states if s[1])
            removable = sum(1 for s in states if s[2])
            self.session_attach_button.setEnabled(False)
            self.session_stop_button.setEnabled(stoppable > 0)
            self.session_remove_button.setEnabled(removable > 0)
            self.session_hint.setText(f"已選 {len(chosen)} 筆：可停止 {stoppable} 筆、可移除紀錄 {removable} 筆。接回終端一次只能選一筆。")
            return
        can_attach, can_stop, can_remove, hint = self.session_actions(chosen[0] if chosen else {})
        self.session_attach_button.setEnabled(can_attach)
        self.session_stop_button.setEnabled(can_stop)
        self.session_remove_button.setEnabled(can_remove)
        self.session_hint.setText(hint)

    def attach_session(self) -> None:
        session = self._selected_record("sessions")
        if session:
            self._invoke("attach", session_id=_value(session, "id"))

    def stop_session(self) -> None:
        chosen = [s for s in self._selected_sessions() if self.session_actions(s)[1]]
        if len(chosen) > 1:
            self._stop_many(chosen)
            return
        session = self._selected_record("sessions")
        if session:
            self._stop_one(session)

    def _stop_one(self, session: dict[str, Any]) -> None:
        if session.get("status") in {"ended", "failed"}:
            self._notice("工作已結束或未啟動成功，可直接移除本機紀錄。")
            return
        name = _value(session, "name", "此工作階段")
        if not self._confirm("停止工作階段", f"確定停止「{name}」？這會中止該工作階段的程序。", confirm_text="停止", danger=True):
            return
        self._invoke("stop", session_id=_value(session, "id"))

    def _stop_many(self, sessions: list[dict[str, Any]]) -> None:
        by_environment: dict[str, list[str]] = {}
        for session in sessions:
            by_environment.setdefault(_value(session, "environment_id"), []).append(session["id"])
        if not self._confirm("停止多個工作階段", f"確定停止選取的 {len(sessions)} 個工作階段？每個環境只會開一個終端連線，一次處理完。",
                             confirm_text="全部停止", danger=True):
            return
        for environment_id, ids in by_environment.items():
            self._invoke("stop", session_ids=ids, environment_id=environment_id)

    def check_unknown_sessions(self) -> None:
        """Local sessions cannot outlive their terminal, so they are simply ended; remote ones are re-queried per host."""
        unknown = [s for s in self._records.get("sessions", []) if _value(s, "status") == "unknown"]
        if not unknown:
            self._toast("沒有狀態不明的工作階段。")
            return
        remote_envs: list[str] = []
        for session in unknown:
            environment = self._by_id("environments", _value(session, "environment_id"))
            if environment.get("kind") == "local":
                self.store.save("sessions", {**session, "status": "ended", "updated_at": runtime_module.now()})
            elif environment.get("id") not in remote_envs:
                remote_envs.append(environment.get("id"))
        self.reload()
        if remote_envs:
            names = "、".join(_value(self._by_id("environments", e), "name", e) for e in remote_envs)
            if len(remote_envs) > 1 and not self._confirm("檢查狀態不明的工作", f"會為這些環境各開一個終端重新確認：{names}。需要驗證時請在終端輸入。", confirm_text="開始檢查"):
                return
            for environment_id in remote_envs:
                self._invoke("refresh", environment_id=environment_id)
        else:
            self._toast("本機狀態不明的工作都已標為已結束。")

    def clean_finished_sessions(self) -> None:
        finished = [s for s in self._records.get("sessions", []) if _value(s, "status") in {"ended", "failed"}]
        if not finished:
            return
        if not self._confirm("清除已結束的紀錄", f"要移除 {len(finished)} 筆已結束或失敗的本機紀錄嗎？不會影響仍在執行的工作。", confirm_text="清除"):
            return
        self._delete_many("sessions", [s["id"] for s in finished])

    def _delete_many(self, kind: str, ids: list[str]) -> None:
        def operation() -> None:
            for record_id in ids:
                try:
                    self.store.delete(kind, record_id)
                except KeyError:
                    pass
        self._run_store(operation, f"已移除 {len(ids)} 筆紀錄。")

    def remove_session(self) -> None:
        chosen = [s for s in self._selected_sessions() if self.session_actions(s)[2]]
        if len(self._selected_sessions()) > 1:
            if not chosen:
                self._notice("選取的工作階段仍在執行，請先停止後再移除紀錄。")
                return
            risky = sum(1 for s in chosen if _value(s, "status") == "unknown")
            message = f"要移除 {len(chosen)} 筆本機紀錄嗎？"
            if risky:
                message += f"\n其中 {risky} 筆狀態不明，移除後會失去在 App 內接回或停止它們的入口（不會停止遠端工作）。"
            if self._confirm("移除本機紀錄", message, confirm_text="移除", danger=bool(risky)):
                self._delete_many("sessions", [s["id"] for s in chosen])
            return
        session = self._selected_record("sessions")
        if session:
            self._remove_one(session)

    def _remove_one(self, session: dict[str, Any]) -> None:
        if session.get("status") == "unknown":
            if not self._confirm("只移除本機紀錄", "無法確認遠端工作是否仍在執行。此操作不會停止遠端工作，且會失去 App 中的重連入口。\n若只是取消首次連線留下的紀錄，可移除後重新啟動。仍要移除嗎？",
                                 confirm_text="仍要移除", danger=True):
                return
        elif session.get("status") not in {"ended", "failed"}:
            self._notice("請先在登入視窗取消或停止工作，再移除紀錄。")
            return
        self._delete("sessions", session["id"])
