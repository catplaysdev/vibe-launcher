"""Per-terminal quota policy editing and safe account controls."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import QDialog

from . import account_check, runtime
from .account_check_dialog import CheckRunner, finding_text
from .i18n import tr
from .kit import ActionMenu, MenuAction, later
from .quota_policy import PolicyStore, ProgressStore, plan_store, quota_flow
from .quota_policy_dialog import QuotaPolicyDialog

# Worker statuses (runtime.ManagedCodexSession.status, runtime.codex_control_status) as the terminal toolbar treats them.
STOP_SWITCH_POLLING = {"needs_attention", "closed", "unmanaged", "failed"}
WARN = {"needs_attention", "unmanaged", "failed", "unknown"}
SWITCH_POLL_MS = 2000       # while a requested switch waits for its safe point ...
SWITCH_POLL_ATTEMPTS = 120  # ... for about four minutes; then the user checks by hand
KEPT_POLICIES = 64          # saved flows of the most recent terminals


class QuotaActions:
    def _quota_policies(self) -> PolicyStore:
        """Each terminal's applied copy of a plan."""
        return PolicyStore(self.store.path.parent)

    def _quota_plans(self) -> PolicyStore:
        """The saved plan library (reusable templates)."""
        return plan_store(self.store.path.parent)

    def _quota_progress(self) -> ProgressStore:
        return ProgressStore(self.store.path.parent)

    def _quota_launch_options(self, session_id: str, environment_id: str) -> dict:
        """What reopening a Codex terminal keeps: its exact conversation and the account it last used (from the local
        worker's own record) and its saved flow. A remote terminal keeps its flow; tmux keeps the rest."""
        options: dict = {}
        environment_id = environment_id or "local"
        if not session_id:
            return options
        state = {}
        if environment_id == "local":
            try:
                state = json.loads(runtime.read_private(runtime.codex_control_path(self.store.path.parent, session_id)
                                                        / "state.json", limit=65536))
            except (OSError, ValueError):
                state = {}
            if not isinstance(state, dict) or state.get("environment_id") != "local":
                state = {}
            account = self._by_id("accounts", state.get("account_id") or "")
            if runtime.switch_supported(account) and runtime.account_fits(account, environment_id):
                options["account_id"] = account["id"]
            try:
                options["resume_thread_id"] = runtime.safe_id(state.get("thread_id"))
            except ValueError:
                pass
        try:
            policy = self._quota_policies().load().get(session_id)
            if policy is None and isinstance(state.get("policy"), dict):
                policy = runtime.checked_quota_policy(state["policy"])
        except (OSError, ValueError) as error:
            # An unusable saved copy means "no plan" for this start, said out loud rather than silently.
            self._toast(tr("The saved quota plans could not be read; this terminal starts without a plan ({error})",
                           error=str(error)[:120]), "warn")
            policy = None
        if policy is not None and policy.environment_id == environment_id and policy.enabled:
            # Accounts removed since the flow was saved drop out; a flow left without accounts is not brought back.
            pool = tuple(identifier for identifier in policy.account_ids
                         if runtime.switch_supported(self._by_id("accounts", identifier) or {})
                         and runtime.account_fits(self._by_id("accounts", identifier), environment_id))
            flow_pools = {item for node in (policy.flow or {}).get("nodes", []) if node.get("type") == "select"
                          for item in node.get("accounts", [])}
            if pool and flow_pools <= set(pool):
                options["quota_policy"] = replace(policy, account_ids=pool).to_dict()
                cursor = self._quota_progress().cursor(session_id)
                if cursor in pool:
                    options["quota_cursor"] = cursor        # its rotation goes on where it stood
            else:
                self._toast(tr("This terminal's quota plan uses accounts that were removed; it starts without a plan."), "warn")
        return options

    def _carry_quota_policy(self, old_session_id: str, new_session_id: str | None) -> None:
        """A reopened terminal is a new session: its saved flow moves with it."""
        if not old_session_id or not new_session_id or old_session_id == new_session_id:
            return
        try:
            store = self._quota_policies()
            policies = store.load()
            policy = policies.pop(old_session_id, None)
            if policy is not None:
                self._save_quota_policies(store, policies, replace(policy, id=new_session_id))
            self._quota_progress().move(old_session_id, new_session_id)
        except (OSError, ValueError) as error:
            self._error(tr("Could not restore quota management"), error)

    @staticmethod
    def _save_quota_policies(store: PolicyStore, policies: dict, policy) -> None:
        policies.pop(policy.id, None)
        policies[policy.id] = policy                 # newest last; the oldest flows are forgotten first
        store.save(dict(list(policies.items())[-KEPT_POLICIES:]))

    def _sync_quota_controls(self) -> None:
        """Terminals that closed stop being watched; a terminal with a saved flow (re-attached, restored) is watched."""
        timers = getattr(self, "_quota_control_timers", {})
        self._quota_control_timers = timers
        polling = getattr(self, "_quota_control_polling", {})
        for tab_id in list(timers):
            tab = self.workspace._tabs.get(tab_id)
            if tab is None or (hasattr(tab, "alive") and not tab.alive()):
                timer = timers.pop(tab_id)
                timer.stop()
                timer.deleteLater()
                polling.pop(tab_id, None)
                getattr(self, "_plan_checks", {}).pop(tab_id, None)
        if getattr(self, "store", None) is None:
            return
        try:
            saved = self._quota_policies().load()
        except (OSError, ValueError):
            return
        for tab_id, tab in list(self.workspace._tabs.items()):
            info = tab.info
            if (tab_id in timers or info.tool not in {"codex", "claude"} or info.kind not in {"agent", "attach"}
                    or info.session_id not in saved or (hasattr(tab, "alive") and not tab.alive())):
                continue
            # Give a starting worker a moment before the first look.
            timers[tab_id] = later(self, 3000, lambda tab_id=tab_id: self._quota_poll(tab_id))

    def _quota_tab(self, tab_id):
        tab = self.workspace._tabs.get(tab_id)
        if not tab or tab.info.tool not in {"codex", "claude"} or not tab.info.session_id:
            self._toast(tr("Account management requires a recorded Codex or Claude terminal"), "warn")
            return None
        return tab

    def _quota_accounts(self, environment_id, tool="codex"):
        """Subscription accounts of one CLI that can take over a conversation in this environment."""
        return [account for account in self._records.get("accounts", [])
                if account.get("tool") == tool and runtime.switch_supported(account)
                and runtime.account_fits(account, environment_id)]

    def _ask_quota_policy(self, dialog):
        return dialog.exec() == QDialog.DialogCode.Accepted

    def edit_terminal_quota(self, tab_id, anchor=None):
        """The terminal's plan: none (the default), one of the saved plans, or the plan editor."""
        tab = self._quota_tab(tab_id)
        if not tab:
            return
        try:
            applied = self._quota_policies().load().get(tab.info.session_id)
            plans = self._terminal_plans(tab)
        except (OSError, ValueError) as error:
            self._error(tr("Could not configure quota management"), error)
            return
        actions = [MenuAction(tr("None: no quota checks, switching or auto continue"), lambda: self._apply_quota_plan(tab_id, None),
                              "check" if applied is None else "")]
        for plan in plans:
            mine = applied is not None and applied.plan_id == plan.id
            text = plan.name
            if mine and applied.revision < plan.revision:
                text = tr("Apply again: {name} (edited since)", name=plan.name)
            elif mine:
                text = tr("{name} (applied)", name=plan.name)
            actions.append(MenuAction(text, lambda plan=plan: self._check_then_apply(tab_id, plan), "check" if mine else ""))
        actions.append(MenuAction(tr("Edit plans…"), lambda: self.open_quota_plans(tab_id), "settings", separator_before=True))
        menu = ActionMenu(actions, self)
        if anchor is not None:
            menu.popup_below(anchor)
        else:
            self._quota_menu = menu             # tests and keyboard callers inspect it

    def _terminal_plans(self, tab) -> list:
        """Saved plans this terminal can use: same environment, accounts of its CLI only."""
        ids = {account["id"] for account in self._quota_accounts(tab.info.environment_id, tab.info.tool)}
        return [plan for plan in self._quota_plans().load().values()
                if plan.environment_id == (tab.info.environment_id or "local") and set(plan.account_ids) <= ids]

    def open_quota_plans(self, tab_id):
        tab = self._quota_tab(tab_id)
        if not tab:
            return
        try:
            accounts = self._quota_accounts(tab.info.environment_id, tab.info.tool)
            if not accounts:
                raise ValueError(tr("No subscription accounts of this CLI in this environment"))
            applied = self._quota_policies().load().get(tab.info.session_id)
            snapshots = {account["id"]: self._quota_report(account["id"], tab.info.environment_id) or {} for account in accounts}
            checker = aligner = None
            if (tab.info.environment_id or "local") == "local":
                # Profiles of this computer are compared ahead of time; a host's are compared there at each handoff.
                root, project, session = self.store.path.parent, self._tab_project(tab), tab.info.session_id
                checker = lambda chosen, reference: account_check.check(chosen, reference, root, project)  # noqa: E731
                aligner = lambda target, reference: account_check.align(target, reference, root, session)  # noqa: E731
            dialog = QuotaPolicyDialog(self, accounts, tab.info.environment_id or "local", tab.info.tool, self._quota_plans(),
                                       applied.plan_id if applied else "", snapshots, own_account=tab.info.account_id,
                                       session_id=tab.info.session_id, checker=checker, aligner=aligner)
            if self._ask_quota_policy(dialog) and dialog.result_plan is not None:
                self._apply_quota_plan(tab_id, dialog.result_plan)
        except (OSError, ValueError) as error:
            self._error(tr("Could not configure quota management"), error)

    def _tab_project(self, tab) -> Path:
        path = self._by_id("presets", tab.info.preset_id).get("path") if tab.info.preset_id else ""
        return Path(path) if path else Path.home()

    def _plan_accounts(self, tab, plan) -> list:
        """The accounts a plan may move this terminal between: its own and every account of a selection."""
        records = {account["id"]: account for account in self._quota_accounts(tab.info.environment_id, tab.info.tool)}
        ids = [tab.info.account_id] + [item for node in quota_flow(plan)["nodes"] if node.get("type") == "select"
                                       for item in node.get("accounts", [])]
        return [records[item] for item in dict.fromkeys(ids) if item in records]

    def _in_background(self, work, done):
        """Run ``work`` off the GUI thread; ``done(result, error)`` runs on it afterwards."""
        runner = CheckRunner(self)

        def finished(_generation, result, error):
            runner.deleteLater()
            done(result, error)
        runner.done.connect(finished)
        runner.run(work)

    def _difference_text(self, findings) -> str:
        names = {account["id"]: account.get("name", account["id"]) for account in self._records.get("accounts", [])}
        items = ["{}: {}".format(names.get(finding.account_id, finding.account_id), finding_text(finding, names))
                 for finding in findings[:3]]
        return "; ".join(items) + (" …" if len(findings) > 3 else "")

    def _check_then_apply(self, tab_id, plan):
        """A saved plan is applied only while its accounts still match (they may have changed since it was saved)."""
        tab = self._quota_tab(tab_id)
        if not tab:
            return
        accounts = self._plan_accounts(tab, plan)
        if (tab.info.environment_id or "local") != "local" or len(accounts) < 2:
            self._apply_quota_plan(tab_id, plan)
            return
        root, project = self.store.path.parent, self._tab_project(tab)
        self._toast(tr("Checking the accounts of {name}…", name=plan.name), "info")

        def done(report, error):
            if error is not None or report is None:
                self._toast(tr("{name} was not applied: the accounts could not be checked ({error})", name=plan.name,
                               error=str(error)[:120]), "warn")
                return
            pending = report.open(plan.accepted_gaps)
            if pending:
                self._toast(tr("{name} was not applied: {count} account differences to resolve in Edit plans… ({items})",
                               name=plan.name, count=len(pending), items=self._difference_text(pending)), "warn")
                return
            self._plan_checks_state(tab_id)["signature"] = report.signature
            self._apply_quota_plan(tab_id, plan)
        self._in_background(lambda: account_check.check(accounts, tab.info.account_id, root, project), done)

    def _plan_checks_state(self, tab_id) -> dict:
        checks = getattr(self, "_plan_checks", {})
        self._plan_checks = checks
        return checks.setdefault(tab_id, {})

    def _watch_plan_accounts(self, tab_id, tab, policy: dict) -> None:
        """While a plan is applied: when a profile it uses changed (sizes and times of its files, looked at with each
        status poll), check again and warn about new differences before an automatic switch runs into them. The
        worker refuses such a switch anyway; this tells the user early."""
        if (tab.info.environment_id or "local") != "local":
            return
        try:
            plan = runtime.checked_quota_policy(policy)
        except ValueError:
            return
        accounts = self._plan_accounts(tab, plan)
        state = self._plan_checks_state(tab_id)
        if len(accounts) < 2 or state.get("running"):
            return
        root, project = self.store.path.parent, self._tab_project(tab)
        if account_check.signature(accounts, root) == state.get("signature"):
            return
        state["running"] = True

        def done(report, error):
            state["running"] = False
            if error is not None or report is None:
                return
            state["signature"] = report.signature
            pending = report.open(plan.accepted_gaps)
            shown = sorted((finding.account_id, finding.key) for finding in pending)
            if pending and shown != state.get("warned"):
                self._toast(tr("{terminal}: the accounts of its quota plan differ now ({items}); an automatic switch to "
                               "them stops. Resolve it in Edit plans….", terminal=tab.info.label,
                               items=self._difference_text(pending)), "warn")
            state["warned"] = shown
        self._in_background(lambda: account_check.check(accounts, tab.info.account_id, root, project, refresh=False), done)

    def _apply_quota_plan(self, tab_id, plan):
        """Copy a saved plan to this terminal (later edits of the plan do not reach it), or remove its copy (None)."""
        tab = self._quota_tab(tab_id)
        if not tab:
            return
        store = self._quota_policies()
        try:
            policies = store.load()
            if plan is None:
                if policies.pop(tab.info.session_id, None) is not None:
                    store.save(policies)
                self._quota_progress().set(tab.info.session_id, None)
                self._quota_control(tab_id, {"control_action": "policy", "policy": None, "accounts": []})
                return
            # The terminal's own account is watched too: a flow decides on its quota.
            watched = tuple(dict.fromkeys(plan.account_ids + ((tab.info.account_id,) if any(
                account["id"] == tab.info.account_id for account in self._quota_accounts(tab.info.environment_id, tab.info.tool))
                else ())))
            copy = runtime.checked_quota_policy(replace(plan, id=tab.info.session_id, account_ids=watched).to_dict())
            if (policies.get(tab.info.session_id) or copy).plan_id != copy.plan_id:
                self._quota_progress().set(tab.info.session_id, None)       # another plan starts its rotation afresh
            self._save_quota_policies(store, policies, copy)
            self._quota_control(tab_id, {"control_action": "policy", "policy": copy.to_dict(), "accounts": list(copy.account_ids)})
        except (OSError, ValueError) as error:
            self._error(tr("Could not configure quota management"), error)

    def switch_terminal_account(self, tab_id, anchor):
        tab = self._quota_tab(tab_id)
        if not tab:
            return
        actions = []
        for account in self._quota_accounts(tab.info.environment_id, tab.info.tool):
            if account["id"] == tab.info.account_id:
                continue
            actions.append(MenuAction(account.get("name", account["id"]),
                                      lambda account_id=account["id"]: self._quota_control(tab_id, {
                                          "control_action": "switch", "target_account_id": account_id,
                                          "accounts": [account_id]}), "user"))
        actions.append(MenuAction(tr("Check handoff status"), lambda: self._quota_control(tab_id, {"control_action": "status"}), "info"))
        actions.append(MenuAction(tr("Cancel the switch and stop automatic switching"),
                                  lambda: self._quota_control(tab_id, {"control_action": "cancel"}), "x"))
        ActionMenu(actions, self).popup_below(anchor)

    def _quota_poll(self, tab_id):
        getattr(self, "_quota_control_timers", {}).pop(tab_id, None)
        current = self.workspace._tabs.get(tab_id)
        if current and (not hasattr(current, "alive") or current.alive()) and not self.workspace.shutting_down:
            self._quota_control(tab_id, {"control_action": "status", "quiet": True})
        else:
            getattr(self, "_quota_control_polling", {}).pop(tab_id, None)

    def _quota_control(self, tab_id, operation):
        tab = self._quota_tab(tab_id)
        if not tab:
            return
        # Only account ids leave the view: the service re-reads their nonsecret identity from the store.
        kwargs = {"account_id": tab.info.account_id, "environment_id": tab.info.environment_id,
                  "session_id": tab.info.session_id, "operation": {key: value for key, value in operation.items() if key != "quiet"}}
        self._start_operation("codex-control", kwargs, lambda result: self._quota_control_result(tab_id, result, operation),
                              quiet=bool(operation.get("quiet")))

    def _quota_control_result(self, tab_id, result, operation=None):
        tab = self.workspace._tabs.get(tab_id)
        if not tab:
            return
        operation = operation or {}
        action = operation.get("control_action")
        status = result.get("status", "unknown")
        accepted = result.get("accepted") is not False and status != "failed"
        try:
            tab.info.resume_thread_id = runtime.safe_id(result["thread_id"])
        except (KeyError, ValueError):
            pass
        switched = False
        account_id = result.get("account_id")
        if account_id and account_id != tab.info.account_id:
            account = self._by_id("accounts", account_id)
            if account and runtime.account_fits(account, tab.info.environment_id):
                tab.info.account_id = account_id
                tab.info.account_name = account.get("name", account_id)
                tab.pane.title.setText(tab.info.label)
                switched = True
        polling = getattr(self, "_quota_control_polling", {})
        self._quota_control_polling = polling
        if action == "switch" and accepted and status not in STOP_SWITCH_POLLING:
            polling[tab_id] = {"target": operation["target_account_id"], "attempts": 0}
        pending = polling.get(tab_id)
        blocked = result.get("blocked") if isinstance(result.get("blocked"), dict) else None
        asked = None
        if pending and blocked and blocked.get("target") == pending["target"]:
            asked = pending["target"]           # the switch the user asked for met account differences: they decide
        if pending and (asked or account_id == pending["target"] or not accepted or status in STOP_SWITCH_POLLING
                        or action == "cancel"):
            polling.pop(tab_id, None)
            pending = None
        if action == "cancel" and accepted:
            try:
                store = self._quota_policies()
                policies = store.load()
                if policies.pop(tab.info.session_id, None) is not None:
                    store.save(policies)
            except (OSError, ValueError) as error:
                self._error(tr("Could not configure quota management"), error)
        if "cursor" in result and (result.get("cursor") is None or isinstance(result.get("cursor"), str)):
            try:
                self._quota_progress().set(tab.info.session_id, result.get("cursor"))
            except (OSError, ValueError):
                pass                    # progress is a convenience; the worker keeps the real position
        policy = result.get("policy")
        # Watch only a live worker that runs a flow; a stopped, unmanaged or vanished worker is not polled again.
        watched = (isinstance(policy, dict) and policy.get("enabled") is True and status not in {"closed", "unmanaged"}
                   and result.get("worker_alive") is not False)
        if pending and pending["attempts"] >= SWITCH_POLL_ATTEMPTS:
            polling.pop(tab_id, None)
            pending = None
            self._toast(tr("Account handoff still pending; check status manually"), "warn")
        self._schedule_quota_poll(tab_id, pending, policy if watched else None)
        if watched and getattr(self, "store", None) is not None:
            self._watch_plan_accounts(tab_id, tab, policy)
        message = tr(result["message"]) if isinstance(result.get("message"), str) and result["message"] else {
            "switching": tr("Account handoff in progress"),
            "running": tr("Task running; account handoff waits for a safe point"),
            "idle": tr("Terminal is ready for a safe account handoff"),
            "queued": tr("Account switch queued until a verified safe handoff point"),
            "starting": tr("Managed Codex terminal is starting"),
            "closed": tr("Managed Codex terminal has closed"),
            "waiting_quota": tr("Waiting for verified quota availability"),
            "unmanaged": tr("Reopen this terminal to enable managed account handoff"),
        }.get(status, tr("Account handoff requires attention; check the terminal status"))
        if accepted and status not in WARN:
            message = {"policy": tr("Quota plan applied to this terminal") if operation.get("policy")
                       else tr("This terminal has no quota plan now"),
                       "cancel": tr("Switch cancelled; automatic switching is off for this terminal")}.get(action, message)
            if switched:
                message = tr("Account switched to {name}; conversation and settings preserved", name=tab.info.account_name)
            elif action == "switch" and pending:
                message = tr("Account switch queued until a verified safe handoff point")
        if asked:
            self._resolve_account_differences(tab_id, asked, blocked.get("gaps") or [])
            return
        if result.get("simulated") and not getattr(tab, "_simulation_noticed", False):
            tab._simulation_noticed = True       # test tooling: say so once per terminal
            self._toast(tr("Simulated quota is active in this terminal (test only)"), "warn")
        notices = getattr(self, "_quota_notice_states", {})
        self._quota_notice_states = notices
        changed = notices.get(tab_id) != (status, message)
        notices[tab_id] = (status, message)
        noteworthy = switched or status in WARN or status == "waiting_quota" or (status in {"idle", "running"} and result.get("message"))
        if not operation.get("quiet") or (changed and noteworthy):
            self._toast(message, "warn" if not accepted or status in WARN else "info")

    def _resolve_account_differences(self, tab_id, target, gaps):
        """Plugins, apps and MCP sign-ins belong to each account: show what the target lacks and let the user switch
        without them, or keep the current account (then install or sign in there, or pick another account)."""
        account = self._by_id("accounts", target) or {}
        rules = [gap.get("name", "") for gap in gaps if isinstance(gap, dict) and gap.get("kind") == "rules"]
        if rules:
            # Approval rules decide what runs without asking: never merged, only aligned on this explicit choice.
            message = tr("The approval rules of {account} differ from this terminal's: {names}\n\nCancel keeps both accounts "
                         "as they are. Aligning gives {account} exactly the current rules; its previous rules are backed "
                         "up first, and it must not be open in another terminal.",
                         account=account.get("name", target), names=", ".join(rules[:8]))
            if self._confirm(tr("Approval rules differ"), message, confirm_text=tr("Align with the current rules")):
                self._quota_control(tab_id, {"control_action": "switch", "target_account_id": target, "accounts": [target],
                                             "align_rules": True})
            else:
                self._toast(tr("Kept the current account"), "info")
            return
        labels = {"plugin": tr("Plugin"), "app": tr("Connected app"), "mcp": tr("MCP sign-in"),
                  "mcp_unknown": tr("MCP sign-in status unknown")}
        lines = "\n".join("• {} — {}".format(labels.get(gap.get("kind"), gap.get("kind", "")), gap.get("name", ""))
                          for gap in gaps[:12] if isinstance(gap, dict))
        message = tr("{account} lacks what this conversation can use here:\n{items}\n\nInstall or sign in on that account and "
                     "switch again, choose another account, or switch now without them.",
                     account=account.get("name", target), items=lines)
        if self._confirm(tr("Account differences"), message, confirm_text=tr("Switch without them")):
            self._quota_control(tab_id, {"control_action": "switch", "target_account_id": target, "accounts": [target],
                                         "allow_gaps": True})
        else:
            self._toast(tr("Kept the current account"), "info")

    def _schedule_quota_poll(self, tab_id, pending, policy):
        timers = getattr(self, "_quota_control_timers", {})
        self._quota_control_timers = timers
        previous = timers.pop(tab_id, None)
        if previous:
            previous.stop()
            previous.deleteLater()
        if not pending and not policy:
            return
        if pending:
            pending["attempts"] += 1
        interval = SWITCH_POLL_MS if pending else max(60, int(policy.get("interval", 120))) * 1000
        timers[tab_id] = later(self, interval, lambda: self._quota_poll(tab_id))
