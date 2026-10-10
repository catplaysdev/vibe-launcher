"""Sessions page: listing, bulk stop/remove/clean and status checks of recorded work.

Mixed into ``VibeLauncherWindow``; methods use the window's own state and helpers.
"""

from __future__ import annotations

from .i18n import tr

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidgetItem, QWidget

from . import env_state
from . import runtime as runtime_module
from .kit import BADGE_ROLE, GROUP_ROLE, ICON_ROLE, INDENT_ROLE, IconButton, label, make_button
from .theme import S, TOOL_ICONS
from .ui_format import TONE_TOKENS, _display_time, _value

LIVE = {"connecting", "waiting_auth", "running", "detached"}
FINISHED = {"ended", "failed"}
OTHER = "__other__"          # group of work whose project was deleted
NOWHERE = "@none"            # work whose project is in no workspace


class SessionActions:
    def _render_sessions(self) -> None:
        if not hasattr(self, "sessions_list"):
            return
        rows = []
        view = self.session_filter.current() if hasattr(self, "session_filter") else "all"
        groups = {"live": LIVE, "unknown": {"unknown"}, "finished": FINISHED}
        shown = []
        for session in sorted(self._records.get("sessions", []), key=lambda s: _value(s, "updated_at"), reverse=True):
            if view in groups and _value(session, "status", "unknown") not in groups[view]:
                continue
            shown.append(session)
            rows.append(self._session_row(session))
        if getattr(self.prefs, "sessions_grouping", "workspace") == "time":
            self._fill_list(self.sessions_list, rows)
        else:
            self._fill_session_groups(self._session_groups(shown, fold_finished=view != "finished"))
        if hasattr(self, "session_grouping"):
            self.session_grouping.set_current(getattr(self.prefs, "sessions_grouping", "workspace"), emit=False)
        self.sessions_panel.show_empty(not rows)
        self._render_session_summary()
        self._sync_session_actions()
        auth = self._records.get("auth_states", [])
        configured = len(self._records.get("accounts", []))
        auth_count = sum(1 for state in auth if _value(state, "status").casefold() in {"authenticated", "logged_in", "ready", "ok"})
        self.auth_summary.setText(tr('ui_sessions.40343f2dca7b', v0=f'{configured}', v1=f'{auth_count}'))

    def _session_row(self, session: dict[str, Any]) -> tuple:
        preset = self._by_id("presets", _value(session, "preset_id"))
        text = f"{_value(session, 'name') or _value(preset, 'name', tr('ui_sessions.eda135194fbc'))}"
        account = self._by_id("accounts", _value(session, "account_id"))
        environment = self._by_id("environments", _value(session, "environment_id"))
        text += (tr('ui_sessions.7c79ac52e29a', v0=f"{_value(environment, 'name')}", v1=f"{_value(account, 'name')}", v2=f"{_display_time(_value(session, 'updated_at'))}", v3=f"{_value(session, 'path')}"))
        badge = env_state.session_badge(_value(session, "status", "unknown"))
        tool = "shell" if _value(session, "mode") == "shell" else _value(account, "tool", "shell")
        return (text, session.get("id"), _value(session, "path"), badge, (TOOL_ICONS.get(tool, "terminal"), TONE_TOKENS.get(badge[1], "neutral")))

    # -- grouped by workspace, then project -------------------------------------------------------------
    def _session_workspace(self, session: dict[str, Any], open_on: dict[str, str], members: dict[str, list[str]]) -> str:
        """The desktop a session belongs to: where its terminal is open, else the first desktop holding its project."""
        if session.get("id") in open_on:
            return open_on[session["id"]]
        places = members.get(_value(session, "preset_id"), [])
        return places[0] if places else NOWHERE

    def _session_groups(self, sessions: list[dict[str, Any]], fold_finished: bool = True) -> list[dict[str, Any]]:
        """Workspaces (picker order, then work in no workspace), each holding its projects, each holding its terminals.

        Inside a workspace, projects with live work come first, then unclear ones, then finished ones (folded)."""
        page = getattr(self, "workspace", None)
        open_on = {tab.info.session_id: tab.info.workspace_id for tab in page.tabs() if tab.info.session_id} if page else {}
        members = self._workspace_membership() if page else {}
        order = page.desktop_ids() if page else [""]
        collapsed = self.__dict__.setdefault("_session_collapsed", {})
        spaces: dict[str, dict[str, Any]] = {}
        for session in sessions:                      # already newest first
            preset = self._by_id("presets", _value(session, "preset_id"))
            place = self._session_workspace(session, open_on, members) if preset else NOWHERE
            space = spaces.setdefault(place, {"key": "ws:" + place, "workspace": place, "projects": {}})
            project = preset.get("id") or OTHER
            group = space["projects"].setdefault(project, {
                "key": f"{place}/{project}", "project": project, "sessions": [],
                "title": _value(preset, "name") if preset else tr("Other work"),
                "environment": _value(self._by_id("environments", _value(preset, "environment_id")), "name")})
            group["sessions"].append(session)
        result = []
        for place in sorted(spaces, key=lambda w: order.index(w) if w in order else len(order) + (w == NOWHERE)):
            space = spaces[place]
            projects = []
            for group in space["projects"].values():
                statuses = [_value(s, "status", "unknown") for s in group["sessions"]]
                live, unknown = sum(1 for s in statuses if s in LIVE), statuses.count("unknown")
                finished = sum(1 for s in statuses if s in FINISHED)
                parts = [tr("{n} active", n=live) if live else "", tr("{n} unknown", n=unknown) if unknown else "",
                         tr("{n} finished", n=finished) if finished else ""]
                group.update(level=1, live=live, unknown=unknown, finished=finished, collapsed_default=live + unknown == 0,
                             meta=" · ".join(filter(None, [group["environment"], *parts])),
                             can_recall=group["project"] != OTHER and bool(self._restore_steps(group["project"])),
                             collapsed=collapsed.get(group["key"], fold_finished and live + unknown == 0),
                             rows=[self._session_row(s) for s in group["sessions"]])
                projects.append(group)
            projects.sort(key=lambda g: (g["live"] == 0, g["collapsed_default"]))    # live, then unclear, then done; stable
            live = sum(g["live"] for g in projects)
            title = tr("Not in any workspace") if place == NOWHERE else (page.workspace_name(place) if page else tr("Default workspace"))
            meta = [tr("{n} project(s)", n=len(projects)), tr("{n} active", n=live) if live else "",
                    tr("Current") if page and place == page.workspace_id else ""]
            result.append({"key": space["key"], "workspace": place, "level": 0, "title": title,
                           "meta": " · ".join(filter(None, meta)), "projects": projects,
                           "collapsed": collapsed.get(space["key"], False)})
        self._session_group_folded = {g["key"]: g["collapsed"] for g in result}
        self._session_group_folded.update({p["key"]: p["collapsed"] for g in result for p in g["projects"]})
        return result

    def _fill_session_groups(self, groups: list[dict[str, Any]]) -> None:
        listing = self.sessions_list
        signature = [("workspace", g["key"], g["title"], g["meta"], g["collapsed"],
                      *([] if g["collapsed"] else [("project", p["key"], p["title"], p["meta"], p["can_recall"], p["finished"],
                                                    p["collapsed"], *(p["rows"] if not p["collapsed"] else [])) for p in g["projects"]]))
                     for g in groups]
        if getattr(listing, "_rows", None) == signature:
            return
        selected = listing.currentItem().data(Qt.ItemDataRole.UserRole) if listing.currentItem() else None
        chosen = {i.data(Qt.ItemDataRole.UserRole) for i in listing.selectedItems()}
        scroll = listing.verticalScrollBar().value()
        listing.blockSignals(True)
        listing.clear()

        def heading(group: dict[str, Any]) -> None:
            item = QListWidgetItem("")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setData(GROUP_ROLE, group["key"])
            item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{group['title']}，{group['meta']}")
            listing.addItem(item)
            listing.setItemWidget(item, self._session_group_header(group))

        for space in groups:
            heading(space)
            if space["collapsed"]:
                continue
            for group in space["projects"]:
                heading(group)
                if group["collapsed"]:
                    continue
                for text, record_id, tip, badge, glyph in group["rows"]:
                    item = QListWidgetItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, record_id)
                    item.setData(ICON_ROLE, glyph)
                    item.setData(BADGE_ROLE, badge)
                    item.setData(INDENT_ROLE, S.xl)       # under its project heading
                    item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{text.splitlines()[0]}，{badge[0]}")
                    item.setToolTip(tip)
                    listing.addItem(item)
                    if record_id == selected:
                        listing.setCurrentItem(item)
                    if record_id in chosen:
                        item.setSelected(True)
        listing.blockSignals(False)
        listing._rows = signature
        listing.verticalScrollBar().setValue(scroll)

    def _session_group_header(self, group: dict[str, Any]) -> QWidget:
        """A workspace heading (its name and icon) or, indented under it, a project heading with its actions."""
        header = QWidget()
        row = QHBoxLayout(header)
        project = group["level"] == 1
        row.setContentsMargins(S.xl if project else 0, 0, 0, 0)
        row.setSpacing(S.sm)
        toggle = IconButton("chevron-right" if group["collapsed"] else "chevron-down",
                            tr("Show") if group["collapsed"] else tr("Hide"), size=28, icon_size=16)
        toggle.clicked.connect(lambda *_, k=group["key"]: self._toggle_session_group(k))
        row.addWidget(toggle)
        page = getattr(self, "workspace", None)
        if not project and page is not None and group["workspace"] != NOWHERE:
            icon = QLabel()
            icon.setPixmap(page.workspace_icon(group["workspace"]).pixmap(18, 18))
            row.addWidget(icon)
        title = label(group["title"], "subsection" if project else "section")
        title.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(title)
        meta = label(group["meta"], "meta")
        meta.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(meta, 1)
        if not project and page is not None and group["workspace"] != NOWHERE:
            row.addWidget(make_button(tr("Switch"), "ghost", "arrow-right", small=True,
                                      callback=lambda *_, w=group["workspace"]: self._show_workspace_from_sessions(w)))
        if project and group["can_recall"]:
            row.addWidget(make_button(tr("Recall all"), "ghost", "repeat", small=True, tip=tr("Re-attach running work and reopen stopped work"),
                                      callback=lambda *_, k=group["project"]: self.restore_project(k)))
        if project and group["finished"]:
            row.addWidget(make_button(tr("Clear finished"), "ghost", "trash", small=True,
                                      callback=lambda *_, k=group["project"]: self.clean_finished_sessions(k)))
        return header

    def _show_workspace_from_sessions(self, workspace_id: str) -> None:
        self.open_workspace(workspace_id)
        self.show_page("workspace")

    def _toggle_session_group(self, key: str) -> None:
        shown = self.__dict__.get("_session_group_folded", {})
        self.__dict__.setdefault("_session_collapsed", {})[key] = not shown.get(key, False)
        self._render_sessions()

    def set_sessions_grouping(self, mode: str) -> None:
        self.prefs.sessions_grouping = "time" if mode == "time" else "workspace"
        self.prefs.save()
        self._render_sessions()

    def session_actions(self, session: dict[str, Any]) -> tuple[bool, bool, bool, str]:
        """(can attach, can stop, can remove, explanation) for a session, from what we actually know about it."""
        if not session:
            return False, False, False, tr('ui_sessions.c60f401d6acc')
        status = _value(session, "status", "unknown")
        local = self._by_id("environments", _value(session, "environment_id")).get("kind") == "local"
        persistent = session.get("persistent", True) is not False
        live = status in {"connecting", "waiting_auth", "running", "detached", "unknown"}
        can_attach = live and not local and persistent and status in {"running", "detached", "unknown"}
        can_stop = live
        can_remove = status in {"ended", "failed", "unknown"}
        if status in {"running", "detached"} and local:
            hint = tr('ui_sessions.89e6b64b9d6d')
        elif status in {"running", "detached"} and persistent:
            hint = tr('ui_sessions.9acd031a456b')
        elif status in {"running", "detached"}:
            hint = tr('ui_sessions.c2a96a21a9a7')
        elif status == "connecting":
            hint = tr('ui_sessions.f685e8865ad5')
        elif status == "waiting_auth":
            hint = tr('ui_sessions.edc46c83e921')
        elif status == "unknown":
            hint = tr('ui_sessions.ded0eb6e72ac')
        elif status == "ended":
            hint = tr('ui_sessions.3d4bd855b8b1')
        elif status == "failed":
            hint = tr('ui_sessions.5028e1ee8b84')
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
        if hasattr(self, "session_filter"):
            self.session_filter.set_counts({"all": counts["total"], "live": counts["live"], "unknown": counts["unknown"],
                                            "finished": counts["finished"]})
        self.session_summary.setText(tr('ui_sessions.ec9f000ab99b', v0=f"{counts['total']}", v1=f"{counts['live']}", v2=f"{counts['unknown']}", v3=f"{counts['finished']}"))
        self.session_check_button.setEnabled(counts["unknown"] > 0)
        self.session_check_button.setText(tr('ui_sessions.3baf062433db', v0=f"{counts['unknown']}") if counts["unknown"] else tr('ui_sessions.c6008e05f901'))
        self.session_clean_button.setEnabled(counts["finished"] > 0)
        self.session_clean_button.setText(tr('ui_sessions.04633d15531c', v0=f"{counts['finished']}") if counts["finished"] else tr('ui_sessions.7c631dac6c1b'))

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
            self.session_hint.setText(tr('ui_sessions.ec46aa943951', v0=f'{len(chosen)}', v1=f'{stoppable}', v2=f'{removable}'))
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
            self._notice(tr('ui_sessions.4f7070c3a3d2'))
            return
        name = _value(session, "name", tr('ui_sessions.c68adc757065'))
        if not self._confirm(tr('ui_sessions.5b3fd961aaac'), tr('ui_sessions.572fcfb14d4f', v0=f'{name}'), confirm_text=tr('ui_sessions.ca4d973c0b00'), danger=True):
            return
        self._invoke("stop", session_id=_value(session, "id"))

    def _stop_many(self, sessions: list[dict[str, Any]]) -> None:
        by_environment: dict[str, list[str]] = {}
        for session in sessions:
            by_environment.setdefault(_value(session, "environment_id"), []).append(session["id"])
        if not self._confirm(tr('ui_sessions.dd8ee86d656b'), tr('ui_sessions.edafd1781990', v0=f'{len(sessions)}'),
                             confirm_text=tr('ui_sessions.75186aec74c8'), danger=True):
            return
        for environment_id, ids in by_environment.items():
            self._invoke("stop", session_ids=ids, environment_id=environment_id)

    def check_unknown_sessions(self) -> None:
        """Local sessions cannot outlive their terminal, so they are simply ended; remote ones are re-queried per host."""
        unknown = [s for s in self._records.get("sessions", []) if _value(s, "status") == "unknown"]
        if not unknown:
            self._toast(tr('ui_sessions.406e8dacc63c'))
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
            if len(remote_envs) > 1 and not self._confirm(tr('ui_sessions.6426d408a587'), tr('ui_sessions.278b7523797f', v0=f'{names}'), confirm_text=tr('ui_sessions.6b41b6dafe4a')):
                return
            for environment_id in remote_envs:
                self._invoke("refresh", environment_id=environment_id)
        else:
            self._toast(tr('ui_sessions.20d6a18d6e0e'))

    def clean_finished_sessions(self, preset_id: str | None = None) -> None:
        """Delete finished records: all of them, or one project's (``OTHER`` = work whose project is gone)."""
        projects = {p.get("id") for p in self._records.get("presets", [])}
        finished = [s for s in self._records.get("sessions", []) if _value(s, "status") in FINISHED
                    and (preset_id is None or (_value(s, "preset_id") if _value(s, "preset_id") in projects else OTHER) == preset_id)]
        if not finished:
            return
        if not self._confirm(tr('ui_sessions.3f8c7de49962'), tr('ui_sessions.f2f32cdcffaa', v0=f'{len(finished)}'), confirm_text=tr('ui_sessions.bce2377283c2')):
            return
        self._delete_many("sessions", [s["id"] for s in finished])

    def _delete_many(self, kind: str, ids: list[str]) -> None:
        def operation() -> None:
            for record_id in ids:
                try:
                    self.store.delete(kind, record_id)
                except KeyError:
                    pass
        self._run_store(operation, tr('ui_sessions.8c9fd7ebc61a', v0=f'{len(ids)}'))

    def remove_session(self) -> None:
        chosen = [s for s in self._selected_sessions() if self.session_actions(s)[2]]
        if len(self._selected_sessions()) > 1:
            if not chosen:
                self._notice(tr('ui_sessions.ab1c2751fc29'))
                return
            risky = sum(1 for s in chosen if _value(s, "status") == "unknown")
            message = tr('ui_sessions.460c48474a10', v0=f'{len(chosen)}')
            if risky:
                message += tr('ui_sessions.3c7e6be15243', v0=f'{risky}')
            if self._confirm(tr('ui_sessions.3f922a7e67a3'), message, confirm_text=tr('ui_sessions.6135d4159e89'), danger=bool(risky)):
                self._delete_many("sessions", [s["id"] for s in chosen])
            return
        session = self._selected_record("sessions")
        if session:
            self._remove_one(session)

    def _remove_one(self, session: dict[str, Any]) -> None:
        if session.get("status") == "unknown":
            if not self._confirm(tr('ui_sessions.b4ec56ecfa03'), tr('ui_sessions.da1df9a9ae34'),
                                 confirm_text=tr('ui_sessions.b941899d0e82'), danger=True):
                return
        elif session.get("status") not in {"ended", "failed"}:
            self._notice(tr('ui_sessions.54e94ec2c040'))
            return
        self._delete("sessions", session["id"])
